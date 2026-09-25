"""Sweep-of-structure precondition for the S020 fvg_confluence modifier.

Anton, 2026-09-18: require the FVG used as confluence to have formed off a
genuine liquidity sweep of a prior swing or "inducement" level, not float
in isolation ("FVG в воздухе" -- an FVG with no such anchor doesn't count
as confluence at all, the touch is skipped like there was no FVG).

Two degrees of structure, both computed directly on entry_tf (deliberately
NOT the higher_tf A/B impulse pivots zones.py already uses for the
retracement zone itself -- this is a separate, smaller-degree structure
check on the same entry_tf bars the FVG lives on):

  "swing"      -- the same ATR-threshold ZigZag as swing.py/s017_elliott,
                  run directly on entry_tf with its own (smaller) ATR
                  multiplier (config.fvg_sweep_swing_atr_mult) -- a
                  confirmed, non-repainting pivot, same primitive already
                  regression-verified for no-look-ahead.
  "inducement" -- a much more local N-bar fractal (config.
                  fvg_sweep_inducement_fractal_bars, default 2): bar i is a
                  fractal high/low if its high/low is strictly beyond both
                  the N bars before and the N bars after it, confirmed at
                  i+N (the earliest point any causal consumer could know
                  it). This is deliberately a *different*, higher-frequency
                  detector than "swing" -- not the same code with a smaller
                  threshold -- because ICT/SMC "inducement" is a distinct,
                  smaller degree of structure (the local pullback high/low
                  that traps early entries), not just "a smaller swing".

A side=LONG confluence check needs a broken LOW (a bullish sweep just
before the reversal that leaves the gap); side=SHORT needs a broken HIGH.
"Broken" means some bar in the gap's own formation window (the
`fvg_sweep_lookback_bars` entry_tf bars ending at the gap's formed_at bar,
inclusive) traded beyond the level's price -- an approximation of "the
impulse that made this FVG is the same impulse that swept the level", not
a bar-by-bar proof they're the identical candle (documented, same honest-
approximation spirit as fvg.py's own simplification).

"Not super old" (Anton's recency condition) is enforced by
`fvg_sweep_max_age_bars`: the swept pivot's confirm_idx must be no more
than that many entry_tf bars before the gap's formation -- i.e. the level
must already have been *known* recently, not necessarily swept recently.
Default chosen pragmatically (not backtested as a free parameter in E3):
50 entry_tf bars (~2 days on H1, ~12.5h on M15) -- open to revision once
E3 numbers are in.
"""
from __future__ import annotations

from typing import List

import numpy as np

from .swing import Pivot, PIVOT_HIGH, PIVOT_LOW, get_swings

__all__ = ["get_swings", "fractal_pivots", "swept_level"]

LONG, SHORT = 1, -1


def fractal_pivots(highs: np.ndarray, lows: np.ndarray, n: int) -> List[Pivot]:
    """Local N-bar fractal highs/lows ("inducement" degree of structure).
    Confirmed causally at i+n -- the earliest bar a lag-n-each-side fractal
    is knowable. Chronological order (by confirm_idx) is preserved."""
    pivots: List[Pivot] = []
    length = len(highs)
    for i in range(n, length - n):
        left_hi, right_hi = highs[i - n:i], highs[i + 1:i + n + 1]
        if highs[i] > left_hi.max() and highs[i] > right_hi.max():
            pivots.append(Pivot(i, highs[i], PIVOT_HIGH, i + n))
        left_lo, right_lo = lows[i - n:i], lows[i + 1:i + n + 1]
        if lows[i] < left_lo.min() and lows[i] < right_lo.min():
            pivots.append(Pivot(i, lows[i], PIVOT_LOW, i + n))
    pivots.sort(key=lambda p: p.confirm_idx)
    return pivots


def swept_level(entry_highs: np.ndarray, entry_lows: np.ndarray,
                pivots: List[Pivot], side: int, gap_formed_at: int,
                lookback_bars: int, max_age_bars: int) -> bool:
    """True if the most recent-enough prior pivot on the correct side for
    `side` (a LOW for LONG, a HIGH for SHORT) was traded through in the
    `lookback_bars` bars ending at (and including) `gap_formed_at`."""
    if gap_formed_at < 0:
        return False
    need_kind = PIVOT_LOW if side == LONG else PIVOT_HIGH
    win_start = max(0, gap_formed_at - lookback_bars)
    # The level must already be KNOWN (confirm_idx) before the sweep window
    # starts, and recent enough -- else it's either not usable yet (would be
    # a look-ahead) or "too old" per Anton's condition.
    candidates = [p for p in pivots if p.kind == need_kind
                  and p.confirm_idx <= win_start
                  and gap_formed_at - p.confirm_idx <= max_age_bars]
    if not candidates:
        return False
    level = candidates[-1].price  # chronological list -> most recent qualifying
    window_hi = entry_highs[win_start:gap_formed_at + 1]
    window_lo = entry_lows[win_start:gap_formed_at + 1]
    if side == LONG:
        return window_lo.min() < level
    return window_hi.max() > level
