"""Background monitoring loop.

Every `check_interval_seconds`:
  1. read 24h volume and last price of all USDT perpetual futures (one request),
  2. keep 1-minute candles of the coins that pass the volume filter up to date (incremental),
  3. recalculate the cascades of a coin every `levels_refresh_minutes` (one request per coin),
  4. build the two lists and notify about coins that are NEW in the "approaching" list.

Settings are re-read on every cycle, so changes saved in the settings window apply at the next check.
"""

from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable

from .analyzer import LevelRow, build_row, candidate_symbols, split_rows
from .binance_api import RateLimitError
from .candles import CandleStore
from .journal import Journal, evaluate_signal
from .levels import Level, average_candle_range, break_percent_for, find_levels, tolerance_percent_for
from .settings import Settings

MAX_JOURNAL_CHECKS_PER_CYCLE = 10

SYMBOLS_REFRESH_SEC = 600   # exchangeInfo is large, so the symbol list is refreshed rarely
RETRY_AFTER_ERROR_SEC = 60  # wait this long after a failed cycle
WARN_AFTER_FAILED_CYCLES = 3
MIN_CHECK_INTERVAL_SEC = 15


class Monitor(threading.Thread):
    def __init__(
        self,
        api,
        load_settings: Callable[[], Settings],
        on_results: Callable[[list[LevelRow], list[LevelRow], dict[str, float], float], None],
        on_alert: Callable[[LevelRow, Settings], None],
        log: Callable[[str], None],
        set_status: Callable[[str], None],
        warn: Callable[[str, str], None] | None = None,
        state_path: Path | None = None,
        journal: Journal | None = None,
    ) -> None:
        super().__init__(name="monitor", daemon=True)
        self.api = api
        self.load_settings = load_settings
        self.on_results = on_results
        self.on_alert = on_alert
        self.log = log
        self.set_status = set_status
        self.warn = warn
        self.journal = journal

        self.store = CandleStore(api)
        self._symbols: set[str] = set()
        self._symbols_at: float | None = None
        self._levels: dict[str, tuple[float, list[Level]]] = {}   # symbol -> (calculated at, cascades)
        self._level_candles: dict[str, list] = {}                  # symbol -> closed candles used for the chart
        self._prices: dict[str, float] = {}
        self._levels_signature: tuple = ()
        self.state_path = state_path
        self._last_alert: dict[str, float] = self._load_alert_times()
        self._since: dict[str, float] = {}      # "SYMBOL:side" -> when it entered the approaching list
        self._failed_cycles = 0
        self._connection_lost = False
        self._stopping = threading.Event()
        self._check_now = threading.Event()

    # ------------------------------------------------------------------ cooldown memory

    def _load_alert_times(self) -> dict[str, float]:
        """Last notification time of every coin. Stored on disk, so the cooldown survives Stop/Start and restarts."""
        if self.state_path is None:
            return {}
        try:
            data = json.loads(Path(self.state_path).read_text(encoding="utf-8"))
            return {str(k): float(v) for k, v in data.items()}
        except (OSError, ValueError, AttributeError):
            return {}

    def _save_alert_times(self, now: float) -> None:
        if self.state_path is None:
            return
        self._last_alert = {k: v for k, v in self._last_alert.items() if now - v < 7 * 86400}
        try:
            path = Path(self.state_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(self._last_alert), encoding="utf-8")
        except OSError:
            pass

    def stop(self) -> None:
        self._stopping.set()
        self._check_now.set()

    def check_now(self) -> None:
        self._check_now.set()

    def run(self) -> None:
        self.log("Monitoring started")
        while not self._stopping.is_set():
            settings = self.load_settings()
            wait_sec = max(MIN_CHECK_INTERVAL_SEC, settings.check_interval_seconds)
            try:
                self.cycle(settings, time.time())
                self._cycle_ok()
            except RateLimitError as exc:
                wait_sec = max(RETRY_AFTER_ERROR_SEC, exc.retry_after)
                self._cycle_failed(f"{exc}; waiting {wait_sec:g} s")
            except Exception as exc:  # network errors, bad responses, ...
                wait_sec = min(wait_sec, RETRY_AFTER_ERROR_SEC)
                self._cycle_failed(f"{exc} (retry in {wait_sec:g} s)")
            self._check_now.wait(wait_sec)
            self._check_now.clear()
        self.set_status("Stopped")
        self.log("Monitoring stopped")

    # ------------------------------------------------------------------ connection watchdog

    def _cycle_ok(self) -> None:
        self._failed_cycles = 0
        if self._connection_lost:
            self._connection_lost = False
            self.log("Connection restored")
            self._warn("Connection restored", "Market data from Binance is flowing again.")

    def _cycle_failed(self, text: str) -> None:
        self._failed_cycles += 1
        self.log(f"Error: {text}")
        self.set_status(f"Error: {text}")
        if self._failed_cycles >= WARN_AFTER_FAILED_CYCLES and not self._connection_lost:
            self._connection_lost = True
            self._warn(
                "Connection lost",
                "Binance data could not be loaded several times in a row. "
                "The lists are NOT being updated. Check the internet / VPN.",
            )

    def _warn(self, title: str, text: str) -> None:
        if self.warn is None:
            return
        try:
            self.warn(title, text)
        except Exception as exc:
            self.log(f"Could not show warning: {exc}")

    # ------------------------------------------------------------------ cascades

    def _refresh_levels(self, settings: Settings, symbols: list[str], now: float) -> tuple[int, RateLimitError | None]:
        """Recalculate the cascades of coins whose cascades are missing or older than the refresh period.
        Returns (number of coins that failed, rate limit error or None)."""
        signature = (
            settings.level_interval, settings.level_lookback_candles, settings.swing_strength,
            settings.min_extremes, settings.level_tolerance_percent, settings.tolerance_mode,
            settings.level_tolerance_candles, settings.level_break_candles, settings.swing_confirm_candles,
        )
        if signature != self._levels_signature:
            self._levels.clear()
            self._levels_signature = signature
        for symbol in list(self._levels):
            if symbol not in symbols:
                del self._levels[symbol]
                self._level_candles.pop(symbol, None)

        max_age = max(1, settings.levels_refresh_minutes) * 60
        stale = [s for s in symbols if s not in self._levels or now - self._levels[s][0] >= max_age]
        now_ms = int(now * 1000)
        limit = max(10, settings.level_lookback_candles) + 1

        def work(symbol: str):
            try:
                raw = self.api.klines(symbol, limit, interval=settings.level_interval)
                closed = [c for c in raw if c.close_time < now_ms]   # drop the candle that is still forming
                price = self._prices.get(symbol, 0.0)
                avg_range = average_candle_range(self.store.get(symbol), settings.avg_candle_minutes)
                tolerance = tolerance_percent_for(
                    settings.tolerance_mode, settings.level_tolerance_percent,
                    settings.level_tolerance_candles, avg_range, price,
                )
                break_percent = break_percent_for(
                    settings.tolerance_mode, tolerance, settings.level_break_candles, avg_range, price
                )
                levels = find_levels(closed, settings.swing_strength, tolerance, settings.min_extremes, break_percent,
                                     settings.swing_confirm_candles)
                self._level_candles[symbol] = closed
                self._levels[symbol] = (now, levels)
                return None
            except Exception as exc:  # one bad coin must not stop the others
                return exc

        failed = 0
        limit_error: RateLimitError | None = None
        with ThreadPoolExecutor(max_workers=6) as pool:
            for result in pool.map(work, stale):
                if result is None:
                    continue
                failed += 1
                if isinstance(result, RateLimitError):
                    limit_error = result
        return failed, limit_error

    # ------------------------------------------------------------------ one check

    def cycle(self, settings: Settings, now: float) -> None:
        started = time.time()
        if self._symbols_at is None or now - self._symbols_at >= SYMBOLS_REFRESH_SEC:
            self._symbols = self.api.usdt_perpetual_symbols()
            self._symbols_at = now
        tickers = self.api.tickers()
        if not tickers:
            raise RuntimeError("empty ticker data")
        volumes = {s: v for s, (v, _p) in tickers.items()}
        self._prices = {s: p for s, (_v, p) in tickers.items()}

        candidates = candidate_symbols(settings, volumes, self._symbols)
        keep = max(settings.avg_candle_minutes, settings.activity_window_minutes) + 1
        updated, failed, limit_error = self.store.update(candidates, keep, int(now * 1000))
        if limit_error is not None:
            raise limit_error
        if candidates and updated == 0:
            raise RuntimeError("could not load candles for any coin")

        level_failed, limit_error = self._refresh_levels(settings, candidates, now)
        if limit_error is not None:
            raise limit_error
        if failed or level_failed:
            self.log(
                f"Data could not be loaded for {max(failed, level_failed)} of {len(candidates)} coins "
                "(will retry next check)"
            )

        rows: list[LevelRow] = []
        for symbol in candidates:
            cached = self._levels.get(symbol)
            if cached is None or not cached[1]:
                continue
            row = build_row(settings, symbol, tickers[symbol][1], volumes[symbol], self.store.get(symbol), cached[1])
            if row is not None:
                rows.append(row)
        near, closest = split_rows(settings, rows)

        # remember when each coin entered the approaching list
        current = {row.key for row in near}
        for key in list(self._since):
            if key not in current:
                del self._since[key]
        for key in current:
            self._since.setdefault(key, now)

        self.on_results(near, closest, dict(self._since), now)

        # notify about coins that are NEW in the approaching list (and not in cooldown)
        cooldown = max(0, settings.cooldown_minutes) * 60
        for row in near:
            last = self._last_alert.get(row.key)
            if last is not None and now - last < cooldown:
                continue
            self._last_alert[row.key] = now
            self._save_alert_times(now)
            self.log(
                f"ALERT {row.symbol}: {row.side} {row.level:g}, {row.distance_percent:.2f}% away "
                f"({row.distance_candles:.2f} candles), {row.touches} extremes"
            )
            self._record_signal(row, settings, now)
            try:
                self.on_alert(row, settings)
            except Exception as exc:  # a notification problem must not stop monitoring
                self.log(f"Could not show notification: {exc}")

        self._evaluate_journal(settings, now)

        total_levels = sum(len(v[1]) for v in self._levels.values())
        self.set_status(
            f"Checked {len(candidates)} coins in {time.time() - started:.0f} s at {time.strftime('%H:%M:%S')}. "
            f"Cascades: {total_levels}. Approaching: {len(near)}. Next check in {settings.check_interval_seconds} s."
        )

    # ------------------------------------------------------------------ chart and journal

    def chart_data(self, symbol: str, count: int):
        """(closed candles of the level interval, cascades, last price) of a coin for the mini chart."""
        candles = self._level_candles.get(symbol, [])
        cached = self._levels.get(symbol)
        levels = list(cached[1]) if cached else []
        return list(candles[-max(10, count):]), levels, self._prices.get(symbol)

    def _record_signal(self, row: LevelRow, settings: Settings, now: float) -> None:
        if self.journal is None:
            return
        self.journal.add(
            {
                "id": f"{row.key}:{int(now)}",
                "ts": now,
                "symbol": row.symbol,
                "side": row.side,
                "level": row.level,
                "price": row.price,
                "distance_percent": round(row.distance_percent, 4),
                "distance_candles": round(row.distance_candles, 3),
                "score": row.score,
                "trend": row.trend,
                "avg_range": row.avg_range,
                "tolerance_abs": row.tolerance_abs,
                "check_minutes": settings.journal_check_minutes,
            }
        )

    def _evaluate_journal(self, settings: Settings, now: float) -> None:
        """Decide the outcome of signals whose evaluation period is over (1-minute candles, one request each)."""
        if self.journal is None:
            return
        for entry in self.journal.pending(now)[:MAX_JOURNAL_CHECKS_PER_CYCLE]:
            start_ms = int(float(entry["ts"]) // 60) * 60_000
            end_ms = start_ms + int(entry.get("check_minutes", 30) + 1) * 60_000
            try:
                raw = self.api.klines(entry["symbol"], int(entry.get("check_minutes", 30)) + 3, start_time=start_ms)
            except RateLimitError:
                return
            except Exception as exc:
                self.log(f"Journal: could not evaluate {entry.get('symbol')}: {exc}")
                continue
            candles = [c for c in raw if start_ms <= c.open_time < end_ms and c.close_time < now * 1000]
            if not candles:
                continue
            outcome = evaluate_signal(entry, candles)
            self.journal.set_outcome(entry["id"], outcome, now)
            self.log(f"Journal: {entry['symbol']} {entry['side']} {entry['level']:g} -> {outcome}")
