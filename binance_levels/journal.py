"""Journal of signals and what the price did afterwards.

Every notification is recorded. `check_minutes` later the outcome is evaluated on 1-minute candles:
  * bounced     - the price reached the cascade and then moved away from it by at least 2 average candles;
  * broke       - a candle closed beyond the cascade (further than its tolerance);
  * touched     - the price reached the cascade but neither bounced nor broke through within the period;
  * not_reached - the price never got to the cascade.
The first event in time wins. "Reached" means within a quarter of an average candle of the cascade.
"""

from __future__ import annotations

import csv
import json
import threading
import time
from pathlib import Path
from typing import Optional, Sequence

from .binance_api import Candle

BOUNCED = "bounced"
BROKE = "broke"
TOUCHED = "touched"
NOT_REACHED = "not_reached"
OUTCOMES = [BOUNCED, BROKE, TOUCHED, NOT_REACHED]

MAX_ENTRIES = 3000
TOUCH_MARGIN_CANDLES = 0.25
BOUNCE_CANDLES = 2.0

CSV_FIELDS = [
    "time", "symbol", "side", "level", "price", "distance_percent", "distance_candles", "score", "trend",
    "outcome", "evaluated_at",
]


def evaluate_signal(entry: dict, candles: Sequence[Candle]) -> str:
    """Outcome of one signal. `candles` are the closed 1-minute candles of the evaluation period, oldest first."""
    level = float(entry["level"])
    avg = float(entry.get("avg_range") or 0.0)
    tol = float(entry.get("tolerance_abs") or 0.0)
    sign = 1.0 if entry["side"] == "resistance" else -1.0   # resistance: price comes from below
    touched = False
    for c in candles:
        extreme = c.high if sign > 0 else c.low
        if sign * (c.close - level) > tol:
            return BROKE
        if sign * (extreme - level) >= -TOUCH_MARGIN_CANDLES * avg:
            touched = True
        if touched and sign * (level - c.close) >= BOUNCE_CANDLES * avg:
            return BOUNCED
    return TOUCHED if touched else NOT_REACHED


def summarize(entries: Sequence[dict]) -> dict:
    """Counts of outcomes (total and per side) and the bounce rate among decided signals."""
    result: dict = {"total": len(entries), "pending": 0}
    for side in ("all", "resistance", "support"):
        result[side] = {o: 0 for o in OUTCOMES}
    for e in entries:
        outcome = e.get("outcome")
        if outcome not in OUTCOMES:
            result["pending"] += 1
            continue
        result["all"][outcome] += 1
        if e.get("side") in ("resistance", "support"):
            result[e["side"]][outcome] += 1
    for side in ("all", "resistance", "support"):
        decided = result[side][BOUNCED] + result[side][BROKE]
        result[side]["bounce_rate"] = result[side][BOUNCED] / decided * 100.0 if decided else None
    return result


class Journal:
    """Thread-safe list of signals saved in a JSON file."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path is not None else None
        self._lock = threading.Lock()
        self._entries: list[dict] = self._load()

    def _load(self) -> list[dict]:
        if self.path is None:
            return []
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return [e for e in data if isinstance(e, dict)]
        except (OSError, ValueError, TypeError):
            return []

    def _save(self) -> None:
        if self.path is None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._entries), encoding="utf-8")
            tmp.replace(self.path)
        except OSError:
            pass

    def add(self, entry: dict) -> None:
        with self._lock:
            entry = dict(entry)
            entry.setdefault("outcome", None)
            entry.setdefault("evaluated_at", None)
            self._entries.append(entry)
            del self._entries[:-MAX_ENTRIES]
            self._save()

    def entries(self) -> list[dict]:
        with self._lock:
            return [dict(e) for e in self._entries]

    def pending(self, now: float) -> list[dict]:
        """Signals whose evaluation period (plus one minute for the last candle to close) is over."""
        with self._lock:
            return [
                dict(e) for e in self._entries
                if e.get("outcome") not in OUTCOMES
                and now - float(e.get("ts", now)) >= (int(e.get("check_minutes", 30)) + 1) * 60
            ]

    def set_outcome(self, signal_id: str, outcome: str, now: float) -> None:
        with self._lock:
            for e in self._entries:
                if e.get("id") == signal_id:
                    e["outcome"] = outcome
                    e["evaluated_at"] = now
                    break
            self._save()

    def export_csv(self, path: Path) -> int:
        """Write all signals to a CSV file; returns the number of rows."""
        rows = self.entries()
        with Path(path).open("w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
            writer.writeheader()
            for e in rows:
                row = dict(e)
                row["time"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(e.get("ts", 0))))
                ev = e.get("evaluated_at")
                row["evaluated_at"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(ev))) if ev else ""
                row["outcome"] = e.get("outcome") or "pending"
                writer.writerow(row)
        return len(rows)
