"""Geometry of the mini chart (pure functions, no tkinter): candles, cascades and the current price.

Every extreme of a cascade is drawn as its own plain line (ray) that starts at that extreme and runs to the right edge,
with a small circle on the extreme. A cascade is therefore a stack of lines.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

from .binance_api import Candle
from .levels import Level


@dataclass
class LineScene:
    y: float                  # the line
    price: float
    side: str
    score: float
    touches: int
    x_start: float            # x of the oldest extreme on this price (the ray starts here)
    points: list = field(default_factory=list)   # (x, y) of every extreme that is visible
    nearest: bool = False     # the line the price is approaching (nearest one ahead of it)


@dataclass
class Scene:
    low: float = 0.0
    high: float = 0.0
    candles: list = field(default_factory=list)   # (x, y_high, y_low, y_open, y_close, is_up)
    lines: list = field(default_factory=list)     # LineScene
    price_y: float | None = None
    body_width: float = 4.0


def build_scene(
    candles: Sequence[Candle],
    levels: Sequence[Level],
    price: float | None,
    width: float,
    height: float,
    margin_left: float = 8.0,
    margin_right: float = 130.0,
    margin_y: float = 10.0,
) -> Scene:
    """Map candles / cascades / price to canvas coordinates (y grows downwards).

    The vertical range covers the candles and the current price (plus 3 % padding); cascades outside it are not drawn."""
    scene = Scene()
    candles = list(candles)
    if not candles or width <= margin_left + margin_right or height <= 2 * margin_y:
        return scene
    low = min(c.low for c in candles)
    high = max(c.high for c in candles)
    if price:
        low, high = min(low, price), max(high, price)
    pad = (high - low) * 0.03 or high * 0.001
    low, high = low - pad, high + pad
    scene.low, scene.high = low, high

    def y_of(value: float) -> float:
        return margin_y + (high - value) / (high - low) * (height - 2 * margin_y)

    slot = (width - margin_left - margin_right) / len(candles)
    scene.body_width = max(1.0, min(slot * 0.7, 12.0))
    x_by_time: dict[int, float] = {}
    for i, c in enumerate(candles):
        x = margin_left + slot * (i + 0.5)
        x_by_time[c.open_time] = x
        scene.candles.append((x, y_of(c.high), y_of(c.low), y_of(c.open), y_of(c.close), c.close >= c.open))

    first_x = margin_left
    for level in levels:
        by_price: dict[float, list[float]] = {}   # price -> x of every extreme at exactly this price
        for t, p in level.points:
            if low <= p <= high and t in x_by_time:
                by_price.setdefault(p, []).append(x_by_time[t])
        if not by_price and not level.points and low <= level.price <= high:
            by_price[level.price] = []            # a cascade without remembered extremes: one line from the left edge
        for p, xs in by_price.items():
            y = y_of(p)
            scene.lines.append(LineScene(
                y=y, price=p, side=level.side, score=level.score, touches=level.touches,
                x_start=min(xs) if xs else first_x, points=[(x, y) for x in xs],
            ))
    if price:
        scene.price_y = y_of(price)
        ahead = [
            ln for ln in scene.lines
            if (ln.side == "resistance" and ln.price >= price) or (ln.side == "support" and ln.price <= price)
        ]
        if ahead:
            min(ahead, key=lambda ln: abs(ln.price - price)).nearest = True
    return scene
