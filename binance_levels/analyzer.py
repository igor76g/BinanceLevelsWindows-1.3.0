"""Applies the coin filters and builds the rows of the two lists."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

from .binance_api import Candle
from .levels import AWAY, Approach, Level, average_candle_range, nearest_approach
from .metrics import natr_values
from .settings import Settings


@dataclass
class LevelRow:
    symbol: str
    side: str
    level: float
    price: float
    distance_percent: float
    distance_candles: float     # distance in "average 1-minute candles"
    touches: int
    volume_24h: float
    natr: float
    trades: int
    score: float = 0.0
    trend: str = "flat"          # closing / away / flat
    avg_range: float = 0.0       # average 1-minute candle range (price units)
    tolerance_abs: float = 0.0   # cascade tolerance (price units)

    @property
    def key(self) -> str:
        """Identity used for the cooldown and the 'since' column."""
        return f"{self.symbol}:{self.side}"


def activity(candles_1m: Sequence[Candle], window: int) -> tuple[float, int]:
    """(NATR %, number of trades) over the last `window` 1-minute candles."""
    window = max(1, int(window))
    recent = list(candles_1m[-(window + 1):])  # one extra candle gives the previous close for the first TR
    if not recent:
        return 0.0, 0
    natr = natr_values(recent)[-window:]
    trades = sum(c.trades for c in recent[-window:])
    return (sum(natr) / len(natr) if natr else 0.0), trades


def candidate_symbols(settings: Settings, volumes_24h: dict[str, float], symbols: set[str]) -> list[str]:
    return sorted(s for s in symbols if volumes_24h.get(s, 0.0) > settings.min_volume_usdt)


def build_row(
    settings: Settings,
    symbol: str,
    price: float,
    volume_24h: float,
    candles_1m: Sequence[Candle],
    levels: Sequence[Level],
) -> Optional[LevelRow]:
    """Row for one coin, or None if it fails a filter or has no usable cascade."""
    if volume_24h <= settings.min_volume_usdt:
        return None
    natr, trades = activity(candles_1m, settings.activity_window_minutes)
    if natr < settings.min_natr or trades < settings.min_trades:
        return None
    avg_range = average_candle_range(candles_1m, settings.avg_candle_minutes)
    levels = [l for l in levels if l.score >= settings.min_level_score]
    price_before = None
    lookback = max(1, settings.approach_lookback_minutes)
    if len(candles_1m) > lookback:
        price_before = candles_1m[-(lookback + 1)].close   # close of the candle `lookback` minutes ago
    approach: Optional[Approach] = nearest_approach(
        levels, price, avg_range, price_before, settings.away_tolerance_candles
    )
    if approach is None:
        return None
    return LevelRow(
        symbol=symbol,
        side=approach.level.side,
        level=approach.line_price,
        price=price,
        distance_percent=approach.distance_percent,
        distance_candles=approach.distance_candles,
        touches=approach.level.touches,
        volume_24h=volume_24h,
        natr=natr,
        trades=trades,
        score=approach.level.score,
        trend=approach.trend,
        avg_range=avg_range,
        tolerance_abs=approach.line_price * approach.level.tolerance_percent / 100.0,
    )


def split_rows(settings: Settings, rows: Sequence[LevelRow]) -> tuple[list[LevelRow], list[LevelRow]]:
    """(coins that are close enough and not moving away, the N coins closest to a cascade but not farther than
    `closest_max_candles`), nearest first."""
    ordered = sorted(rows, key=lambda r: r.distance_candles)
    near = [
        r for r in ordered
        if r.distance_candles <= settings.approach_candles
        and not (settings.approach_filter_enabled and r.trend == AWAY)   # skip coins that are moving away
    ]
    closest = [
        r for r in ordered
        if settings.closest_max_candles <= 0 or r.distance_candles <= settings.closest_max_candles
    ]
    return near, closest[: max(1, settings.closest_count)]
