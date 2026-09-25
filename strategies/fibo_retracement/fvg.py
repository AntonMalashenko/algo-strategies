"""Rolling fair-value-gap (FVG) tracker -- S020 "fvg_confluence" modifier.

Same causal 3-candle-gap definition already used elsewhere in this project
(strategies/fvg_mtf.py::find_h4_fvg, strategies/ger40_lonfra/structure.py's
bull/bear rolling levels): at bar t, a bullish gap confirms when
low[t] > high[t-2] (pattern bars t-2, t-1, t), zone = [high[t-2], low[t]];
a bearish gap is the mirror. The zone is knowable starting at bar t itself
(bar t must CLOSE to confirm it) -- callers must not use it before that.

This is a fresh, timeframe-agnostic implementation rather than a reuse of
find_h4_fvg, because that function hardcodes a 4h "avail" offset tied to
H4 bars; S020's entry_tf can be H1 or M15. The underlying 3-bar pattern
match is identical -- only the "when is this knowable" bookkeeping differs.

Simplification (still documented, same one ger40_lonfra/structure.py accepts
for its cb/cr levels): only the MOST RECENT gap per direction is tracked --
a second, older, still-open gap on the same side is never resurrected once
a fresher one forms. That part of the original simplification stands.

MITIGATION TRACKING (added 2026-09-18, Anton -- "не брать пустые фвг"): the
*_top/_bot arrays below are unchanged and keep including gaps regardless of
whether they have since been fully filled (this is what
strategy-passport-S020.md E2 / FVG_CONFLUENCE_S020 was backtested against --
preserved byte-for-byte so that reference result stays reproducible). New
parallel *_top_um/_bot_um ("unmitigated") arrays additionally go NaN once a
later bar's price fully closes back through the gap's far edge (bull: a low
below the gap's bottom edge; bear: a high above the gap's top edge) -- an
"empty" gap, in Anton's phrasing, no longer counts as confluence under
these. Selected via fvg_overlaps_zone(..., require_unmitigated=True/False).
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

LONG, SHORT = 1, -1


@dataclass(frozen=True)
class FvgLevels:
    bull_top: np.ndarray       # upper edge of the most recent bullish gap (NaN until one confirms)
    bull_bot: np.ndarray       # lower edge of the most recent bullish gap
    bear_top: np.ndarray       # upper edge of the most recent bearish gap
    bear_bot: np.ndarray       # lower edge of the most recent bearish gap
    bull_top_um: np.ndarray    # same, but NaN once the active gap is fully mitigated ("empty")
    bull_bot_um: np.ndarray
    bear_top_um: np.ndarray
    bear_bot_um: np.ndarray
    bull_formed_at: np.ndarray  # bar index the currently active bullish gap was confirmed at (-1 = none)
    bear_formed_at: np.ndarray


def fvg_levels(highs: np.ndarray, lows: np.ndarray) -> FvgLevels:
    n = len(highs)
    bull_top = np.full(n, np.nan)
    bull_bot = np.full(n, np.nan)
    bear_top = np.full(n, np.nan)
    bear_bot = np.full(n, np.nan)
    bull_top_um = np.full(n, np.nan)
    bull_bot_um = np.full(n, np.nan)
    bear_top_um = np.full(n, np.nan)
    bear_bot_um = np.full(n, np.nan)
    bull_formed_at = np.full(n, -1, dtype=int)
    bear_formed_at = np.full(n, -1, dtype=int)

    cur_bull_top = cur_bull_bot = np.nan
    cur_bear_top = cur_bear_bot = np.nan
    cur_bull_formed = cur_bear_formed = -1
    cur_bull_mitigated = cur_bear_mitigated = False

    for t in range(n):
        i = t - 1
        if i - 1 >= 0:
            if lows[t] > highs[i - 1]:              # bullish gap confirmed at t
                cur_bull_bot, cur_bull_top = highs[i - 1], lows[t]
                cur_bull_formed = t
                cur_bull_mitigated = False
            if highs[t] < lows[i - 1]:               # bearish gap confirmed at t
                cur_bear_top, cur_bear_bot = lows[i - 1], highs[t]
                cur_bear_formed = t
                cur_bear_mitigated = False

        # Mitigation check against whatever gap is currently tracked (safe even
        # on the same bar a gap just formed -- formation requires the gap's own
        # bar to be strictly beyond the far edge, so it cannot self-mitigate).
        if not np.isnan(cur_bull_bot) and lows[t] < cur_bull_bot:
            cur_bull_mitigated = True
        if not np.isnan(cur_bear_top) and highs[t] > cur_bear_top:
            cur_bear_mitigated = True

        bull_top[t], bull_bot[t] = cur_bull_top, cur_bull_bot
        bear_top[t], bear_bot[t] = cur_bear_top, cur_bear_bot
        bull_formed_at[t] = cur_bull_formed
        bear_formed_at[t] = cur_bear_formed
        if not cur_bull_mitigated:
            bull_top_um[t], bull_bot_um[t] = cur_bull_top, cur_bull_bot
        if not cur_bear_mitigated:
            bear_top_um[t], bear_bot_um[t] = cur_bear_top, cur_bear_bot

    return FvgLevels(bull_top=bull_top, bull_bot=bull_bot,
                     bear_top=bear_top, bear_bot=bear_bot,
                     bull_top_um=bull_top_um, bull_bot_um=bull_bot_um,
                     bear_top_um=bear_top_um, bear_bot_um=bear_bot_um,
                     bull_formed_at=bull_formed_at, bear_formed_at=bear_formed_at)


def fvg_overlaps_zone(levels: FvgLevels, idx: int, side: int,
                      near: float, far: float,
                      require_unmitigated: bool = False) -> bool:
    """True if the active FVG matching `side` (as of bar `idx`, causal --
    only levels already confirmed at or before idx) overlaps the
    retracement zone [near, far]. side=LONG needs a bullish gap (up-move
    signature = support confluence for a bounce buy); side=SHORT needs a
    bearish gap (resistance confluence for a bounce sell). When
    require_unmitigated, a gap already fully filled back through ("empty")
    does not count."""
    lo_z, hi_z = (near, far) if near <= far else (far, near)
    if side == LONG:
        top, bot = (levels.bull_top_um[idx], levels.bull_bot_um[idx]) if require_unmitigated \
            else (levels.bull_top[idx], levels.bull_bot[idx])
    else:
        top, bot = (levels.bear_top_um[idx], levels.bear_bot_um[idx]) if require_unmitigated \
            else (levels.bear_top[idx], levels.bear_bot[idx])
    if np.isnan(top) or np.isnan(bot):
        return False
    lo_g, hi_g = (bot, top) if bot <= top else (top, bot)
    return lo_g <= hi_z and hi_g >= lo_z


def fvg_formed_at(levels: FvgLevels, idx: int, side: int,
                  require_unmitigated: bool = False) -> int:
    """Bar index the currently active gap (as of `idx`) was confirmed at, or
    -1 if none is active (including: none survives the unmitigated filter)."""
    if side == LONG:
        formed = levels.bull_formed_at[idx]
        alive = not np.isnan(levels.bull_top_um[idx]) if require_unmitigated else not np.isnan(levels.bull_top[idx])
    else:
        formed = levels.bear_formed_at[idx]
        alive = not np.isnan(levels.bear_top_um[idx]) if require_unmitigated else not np.isnan(levels.bear_top[idx])
    return int(formed) if alive else -1
