"""Cascades of price levels.

Definitions
  * A swing high (swing low) is a candle whose high (low) is the highest (lowest) among `strength` candles on its left
    and `confirm` candles on its right (the right side is shorter, so a fresh extreme is recognised quickly).
  * A CASCADE is a group of at least `min_extremes` swing highs (resistance) or swing lows (support) whose prices
    lie within a tolerance of each other.
    The tolerance is either a fixed percent or N "average 1-minute candles" (adapts to the volatility of the coin).
  * Every extreme of a cascade is a LINE (a level). The cascade is the stack of its lines; the extreme line (highest high
    of a resistance, lowest low of a support) decides whether the cascade is broken.
  * The price approaches the NEAREST line of the cascade that is still ahead of it (for a support: the highest low below
    the price; for a resistance: the lowest high above the price).
  * A cascade is dropped as BROKEN when, after its OLDEST extreme, any candle went beyond the level (by its high / low,
    a wick counts) by more than a small buffer. Only levels that the price has never traded through are kept.
  * SCORE (0-100) of a cascade = 50 points for the number of extremes (5 or more = full) + 20 points for how long
    the cascade has existed (first to last extreme, relative to the searched history) + 30 points for the volume
    traded on the extreme candles compared with the usual candle volume (3x or more = full).
  * The distance to a cascade is measured from the current price; it is also expressed in "average 1-minute candles",
    where the average 1-minute candle is the mean (high - low) of the last hour of 1-minute candles.
  * The TREND of the approach compares the distance now with the distance a few minutes ago:
    "closing" (getting closer), "away" (moving away from the cascade) or "flat".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Sequence

from .binance_api import Candle

RESISTANCE = "resistance"
SUPPORT = "support"

CLOSING = "closing"
AWAY = "away"
FLAT = "flat"

MIN_TOLERANCE_PERCENT = 0.05   # limits for the adaptive tolerance, so a quiet or a wild coin cannot break the logic
MAX_TOLERANCE_PERCENT = 2.0


@dataclass
class Level:
    side: str                      # RESISTANCE or SUPPORT
    price: float                   # average price of the extremes
    touches: int                   # number of extremes in the cascade
    last_index: int                # index of the newest extreme
    first_index: int = 0           # index of the oldest extreme
    score: float = 0.0             # 0-100, see the module docstring
    tolerance_percent: float = 0.0  # tolerance that was used to build the cascade
    points: list = field(default_factory=list)   # the extremes: (candle open time in ms, price)

    @property
    def prices(self) -> list[float]:
        """Distinct prices of the lines of the cascade, ascending."""
        values = sorted({p for _t, p in self.points})
        return values or [self.price]

    def line_ahead(self, price: float) -> float | None:
        """The nearest line of the cascade that the price has not reached yet (None if all lines are behind it).
        Resistance: lowest line at or above the price; support: highest line at or below the price."""
        if self.side == RESISTANCE:
            ahead = [p for p in self.prices if p >= price]
            return min(ahead) if ahead else None
        ahead = [p for p in self.prices if p <= price]
        return max(ahead) if ahead else None

    @property
    def zone(self) -> tuple[float, float]:
        """(lowest, highest) price among the extremes of the cascade."""
        prices = [p for _t, p in self.points]
        return (min(prices), max(prices)) if prices else (self.price, self.price)


@dataclass
class Approach:
    level: Level
    line_price: float        # the line of the cascade the price is approaching (nearest one ahead)
    price: float             # current price
    distance: float          # absolute price distance to the level
    distance_percent: float
    distance_candles: float  # distance / average 1-minute candle range
    trend: str = FLAT        # CLOSING / AWAY / FLAT


def tolerance_percent_for(mode: str, percent: float, candles: float, avg_range: float, price: float) -> float:
    """Cascade tolerance in percent of the price. mode 'percent' = fixed; 'candles' = N average 1-minute candles."""
    if mode == "candles" and avg_range > 0 and price > 0:
        value = candles * avg_range / price * 100.0
        return min(MAX_TOLERANCE_PERCENT, max(MIN_TOLERANCE_PERCENT, value))
    return max(0.0, percent)


def break_percent_for(mode: str, tolerance_percent: float, break_candles: float, avg_range: float, price: float) -> float:
    """How far (in percent) a wick may go beyond a level before the level counts as broken.
    mode 'candles': N average 1-minute candles (never more than the tolerance); otherwise a quarter of the tolerance."""
    if mode == "candles" and avg_range > 0 and price > 0:
        return min(max(0.0, tolerance_percent), max(0.0, break_candles * avg_range / price * 100.0))
    return max(0.0, tolerance_percent) * 0.25


def find_swings(
    candles: Sequence[Candle], strength: int, confirm: int | None = None
) -> tuple[list[tuple[int, float]], list[tuple[int, float]]]:
    """Return (swing highs, swing lows) as lists of (candle index, price).

    An extreme must beat `strength` candles on its left and `confirm` candles on its right (default: `strength`)."""
    strength = max(1, int(strength))
    confirm = strength if confirm is None else max(1, int(confirm))
    highs: list[tuple[int, float]] = []
    lows: list[tuple[int, float]] = []
    for i in range(strength, len(candles) - confirm):
        left = candles[i - strength:i]
        right = candles[i + 1:i + 1 + confirm]
        high, low = candles[i].high, candles[i].low
        # strict on the left, non-strict on the right: a flat top is counted once (its first candle)
        if all(high > c.high for c in left) and all(high >= c.high for c in right):
            highs.append((i, high))
        if all(low < c.low for c in left) and all(low <= c.low for c in right):
            lows.append((i, low))
    return highs, lows


def cluster(
    points: Sequence[tuple[int, float]], tolerance_percent: float, min_count: int
) -> list[tuple[float, int, int, list[tuple[int, float]]]]:
    """Group points whose prices are within `tolerance_percent` of the lowest price of the group.
    Returns (average price, count, newest index, members) of every group with at least `min_count` points."""
    ordered = sorted(points, key=lambda p: p[1])
    groups: list[list[tuple[int, float]]] = []
    for point in ordered:
        if groups and point[1] <= groups[-1][0][1] * (1.0 + tolerance_percent / 100.0):
            groups[-1].append(point)
        else:
            groups.append([point])
    result = []
    for group in groups:
        if len(group) >= max(1, min_count):
            result.append((sum(p[1] for p in group) / len(group), len(group), max(p[0] for p in group), group))
    return result


def score_level(touches: int, first: int, last: int, total: int, extreme_volume: float, usual_volume: float) -> float:
    """Strength of a cascade, 0-100 (see the module docstring)."""
    touch_points = min(max(touches, 0), 5) / 5.0 * 50.0
    span_points = 20.0 * (last - first) / max(1, total - 1)
    ratio = extreme_volume / usual_volume if usual_volume > 0 else 0.0
    volume_points = 30.0 * min(max(ratio, 0.0), 3.0) / 3.0
    return round(min(100.0, touch_points + span_points + volume_points), 1)


def find_levels(
    candles: Sequence[Candle], strength: int, tolerance_percent: float, min_extremes: int, break_percent: float = 0.0,
    confirm: int | None = None,
) -> list[Level]:
    """All unbroken cascades found in `candles` (closed candles, oldest first).

    Resistance: level = highest high of the cascade; it is broken if any later candle's high is above
    level * (1 + break_percent). Support: mirrored with lows."""
    highs, lows = find_swings(candles, strength, confirm)
    levels: list[Level] = []
    buffer = break_percent / 100.0
    usual_volume = sum(c.quote_volume for c in candles) / len(candles) if candles else 0.0

    def make(side: str, price: float, members: list[tuple[int, float]]) -> Level:
        first = min(p[0] for p in members)
        last = max(p[0] for p in members)
        volume = sum(candles[p[0]].quote_volume for p in members) / len(members)
        score = score_level(len(members), first, last, len(candles), volume, usual_volume)
        return Level(side, price, len(members), last, first, score, tolerance_percent,
                     [(candles[p[0]].open_time, p[1]) for p in members])

    for _avg, _count, _last, members in cluster(highs, tolerance_percent, min_extremes):
        level = max(p[1] for p in members)
        first = min(p[0] for p in members)
        if all(c.high <= level * (1.0 + buffer) for c in candles[first + 1:]):
            levels.append(make(RESISTANCE, level, members))
    for _avg, _count, _last, members in cluster(lows, tolerance_percent, min_extremes):
        level = min(p[1] for p in members)
        first = min(p[0] for p in members)
        if all(c.low >= level * (1.0 - buffer) for c in candles[first + 1:]):
            levels.append(make(SUPPORT, level, members))
    return levels


def average_candle_range(candles_1m: Sequence[Candle], count: int) -> float:
    """Mean (high - low) of the last `count` 1-minute candles (0 if there are none)."""
    recent = list(candles_1m[-max(1, int(count)):])
    if not recent:
        return 0.0
    return sum(c.high - c.low for c in recent) / len(recent)


def distance_to(side: str, line: float, price: float) -> float:
    """Signed distance: positive while the price is on the 'approaching' side of the line."""
    return line - price if side == RESISTANCE else price - line


def nearest_approach(
    levels: Sequence[Level],
    price: float,
    avg_range: float,
    price_before: Optional[float] = None,
    away_tolerance_candles: float = 0.25,
) -> Approach | None:
    """The line closest to `price` among all cascades, looking only at lines the price has not passed yet.

    A resistance line counts while price <= line (approaching from below); a support line while price >= line.
    `price_before` (the price a few minutes ago) is used for the trend: the distance must change by more than
    `away_tolerance_candles` average candles to count as closing / away.
    Returns None if there is no such line or the average candle range is unknown."""
    if price <= 0 or avg_range <= 0:
        return None
    best: Approach | None = None
    for level in levels:
        line = level.line_ahead(price)
        if line is None:
            continue  # the price is already beyond every line of this cascade
        distance = distance_to(level.side, line, price)
        if best is None or distance < best.distance:
            trend = FLAT
            if price_before is not None and price_before > 0:
                change = (distance - distance_to(level.side, line, price_before)) / avg_range
                if change > away_tolerance_candles:
                    trend = AWAY
                elif change < -away_tolerance_candles:
                    trend = CLOSING
            best = Approach(level, line, price, distance, distance / price * 100.0, distance / avg_range, trend)
    return best
