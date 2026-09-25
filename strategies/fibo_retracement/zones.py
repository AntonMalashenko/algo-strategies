"""Retracement-zone construction for S020 variant (a) -- classic swing.

A "zone" is derived from the last confirmed impulse leg (A -> B) on the
higher timeframe, where B is the most recent confirmed pivot and A is the
pivot immediately before it. The leg's direction is read off B's kind (a
freshly confirmed HIGH means the leg ran up from a LOW A -- a long setup;
a freshly confirmed LOW means a short setup) -- the same "last confirmed
swing in the trend's favor" definition as strategy-passport-S020.md 4.1.

0%/100% anchors (4.1's explicit requirement, the most common place
look-ahead hides): 0% = B (the freshest confirmed extreme), 100% = A (the
impulse origin) -- retracement fraction f means the price level
B + f*(A - B). Both A and B here are only ever pivots the caller
(engine.py) has confirmed via Pivot.confirm_idx before building the zone;
this module does no time-index handling itself, on purpose, so it cannot
smuggle in a look-ahead bug of its own.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

from .swing import Pivot, PIVOT_HIGH, PIVOT_LOW

LONG, SHORT = 1, -1


@dataclass(frozen=True)
class Zone:
    side: int              # LONG | SHORT
    a_price: float         # impulse origin (100% anchor)
    b_price: float         # impulse end, freshest confirmed pivot (0% anchor)
    near: float             # entry_level_min edge -- hit first retracing off B
    far: float              # entry_level_max edge -- deeper retracement
    stop: float             # stop_level edge -- invalidation
    b_confirm_idx: int     # bar index (higher_tf) at which B became known
    impulse_height: float  # |b_price - a_price|, for the TP projection


def build_zone(pivots: List[Pivot], level_min: float, level_max: float,
               stop_level: float) -> Optional[Zone]:
    """Zone from the last two confirmed pivots, or None if not enough history."""
    if len(pivots) < 2:
        return None
    a, b = pivots[-2], pivots[-1]
    side = LONG if b.kind == PIVOT_HIGH else SHORT
    height = abs(b.price - a.price)

    def level(f: float) -> float:
        return b.price - side * f * height

    return Zone(
        side=side, a_price=a.price, b_price=b.price,
        near=level(level_min), far=level(level_max), stop=level(stop_level),
        b_confirm_idx=b.confirm_idx, impulse_height=height,
    )
