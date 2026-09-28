"""Frozen strategy config for S021 -- ORB (opening range breakout) / intraday momentum.

Rules are frozen per claude/strategy-passport-S021.md sec 3 (Anton, 2026-09-21). This is
an independent from-scratch implementation, built by a Cowork session with no access to
Anton's original .../Trading/strategies/ORB-intraday-momentum/ code -- see passport sec 0
for why independent verification, not a review of the original, is the point of this
package. Do not tune these values here; a variant is a new preset via .with_(...)
(strategy-modifiers discipline) -- ORB_BASE itself must stay exactly what the passport
freezes.

session_open/session_close/entry_cutoff are clock readings against histdata's native
FIXED UTC-5 (EST) clock, not DST-converted -- see engine.py's module docstring for the
2026-09-21 investigation that confirmed this is the correct, canonical rule: it anchors
the opening range at the true NY 09:30 open in EST months and at true NY 10:30 in EDT
months (a deliberate seasonal-conditional anchor, not a timezone bug).

S021.1 -- NAS100 Opening-Range Squeeze (claude/prompts-new-strategy-candidates.md P14,
"S021.1"; own reformalization of an idea from an external review (Google Gemini), see the
prompt's preamble). This is a PRESET tracked on this SAME engine -- a new tracking number,
NOT a new strategy/file -- per the 2026-09-24 decisions-log classification. The
squeeze_preset_enabled=False fields below are additive and unused by ORB_BASE/simulate();
NAS_SQUEEZE_PRESET below sets squeeze_preset_enabled=True and is read only by
engine.simulate_squeeze_preset(), a separate sibling function -- see that function's
docstring for why this preset needed its own function rather than a branch inside
simulate(). Session times for the preset are TRUE UTC (unlike ORB_BASE's fixed-EST clock
above) -- see engine.simulate_squeeze_preset()'s docstring.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import time


@dataclass(frozen=True)
class OrbConfig:
    adr_window: int = 14                    # trading days of ADR lookback (past only, causal)
    k_range: float = 0.20                   # opening-range half-width = k_range * ADR14
    stop_adr_mult: float = 0.75             # stop distance = stop_adr_mult * ADR14 from entry
    session_open: time = time(9, 30)        # fixed-EST clock reading (NOT DST-converted -- see module docstring)
    session_close: time = time(15, 59)
    entry_cutoff: time = time(14, 29)       # last minute of the entry scan window
    min_session_bars: int = 350             # day invalid below this many M1 bars in-session
    max_gap_days_per_session: float = 2.5   # skip day if the ADR lookback window is this stretched
    cost_bps_roundtrip: float = 1.0         # round-trip cost, bps of entry price, charged once/trade

    # --- S021.1 "NAS100 Opening-Range Squeeze" preset-only fields (default-off; unused by
    # the base ADR-band simulate()/ORB_BASE path above -- see engine.simulate_squeeze_preset()
    # and NAS_SQUEEZE_PRESET below). Session times here are TRUE UTC, not the fixed-EST clock
    # session_open/session_close/entry_cutoff above use.
    squeeze_preset_enabled: bool = False
    orb_open_start: time = time(13, 30)     # UTC, inclusive -- first M15 candle of the cash session
    orb_open_end: time = time(13, 45)       # UTC, exclusive -- one M15 candle wide (13:30-13:45)
    orb_entry_start: time = time(13, 45)    # UTC, inclusive -- resting-stop entry window
    orb_entry_end: time = time(16, 0)       # UTC, exclusive
    orb_force_close: time = time(20, 0)     # UTC -- forced time exit
    orb_entry_buffer_pts: float = 2.0       # stop order offset beyond ORB_High/Low, index points
    squeeze_atr_period: int = 14            # Daily ATR lookback (causal, strictly prior days)
    squeeze_atr_mult: float = 0.7           # squeeze filter: ORB width < squeeze_atr_mult * Daily ATR14
    trend_ema_period: int = 50              # Daily EMA lookback (causal, strictly prior days)
    orb_tp_r_mult: float = 2.5              # TP = orb_tp_r_mult * stop_dist (ORB_BASE has no TP at all)

    # --- breakeven-stop modifier (default-off; unused by ORB_BASE). Anton asked 2026-09-28
    # whether breakeven had ever been tested on S021, distinct from the S007 breakeven_at_r
    # precedent (strategy-passport-S007.md sec 4d, where it IS part of the live preset). None
    # (the default) reproduces the frozen ORB_BASE path byte for byte -- see
    # backtest/run_s021_breakeven.py's regression check (n=1518, sum_net_pts=16220.614,
    # identical to the pre-modifier baseline).
    #
    # TESTED 2026-09-28 at 0.5R/1.0R/1.5R and REJECTED at all three -- see
    # backtest/run_s021_breakeven.py and backtest-log.md 2026-09-28 for full numbers. Solo
    # (real 1bp cost, 2019-2026, n=1518 trades): BASE mean +0.0713R/trade, WR 53.0%, maxDD
    # -17.09R; BE@0.5R +0.0669R (-6.2% vs BASE), WR 44.0% (-9pp), maxDD -18.65R (worse);
    # BE@1.0R +0.0704R (-1.3%), WR 51.8% (-1.2pp), maxDD -17.82R (mildly worse); BE@1.5R
    # +0.0714R (+0.1%, flat), WR 52.8% (-0.2pp), maxDD -16.09R (mildly better on DD only) --
    # all 8/8 years stay positive at
    # every level, so this is a magnitude call, not a Gate-1 pass/fail. Root cause: S021 has no
    # TP and rides the full move to the 15:59 time-exit -- cutting to breakeven on a pullback
    # sacrifices exactly the winners that later recover and run, while the historical BAD days
    # are dominated by the original stop being hit outright (never reaching +R first), so BE
    # protects almost nothing on the loss side. Gate 3 (solo AND the S007+S021 combo) confirms
    # this is not just an expectancy story: at the live combo risk (S007=0.25%, S021=0.50%) all
    # three BE levels give cashout/bust rates within Monte-Carlo noise of the BASE combo
    # (~99.9%+ cashout, ~0% busts either way) -- breakeven changes essentially nothing for
    # prop-survivability here, because S021's structural safety (1 trade/day, no pyramiding)
    # already keeps the daily-loss lane closed regardless of where the stop sits intraday.
    # Kept as documented off-by-default presets (ORB_BE05/ORB_BE10/ORB_BE15 below), not
    # deleted, per this project's archive-don't-delete convention for rejected modifiers.
    breakeven_at_r: float | None = None     # once favorable excursion >= this many R (multiples of
                                             # stop_dist) is reached, move the stop to entry_price

    def with_(self, **overrides) -> "OrbConfig":
        return replace(self, **overrides)


ORB_BASE = OrbConfig()  # frozen baseline -- matches passport sec 3 exactly, never edit in place

# S021.1 preset -- NAS100 Opening-Range Squeeze, per P14's spec values (all "untested first
# guess" per the prompt, not tuned). Base ORB_BASE is untouched; only squeeze_preset_enabled
# flips, selecting engine.simulate_squeeze_preset() at the call site (backtest runner), not
# a runtime branch inside simulate().
NAS_SQUEEZE_PRESET = ORB_BASE.with_(squeeze_preset_enabled=True)

# Breakeven-stop presets on the BASE ORB_BASE path (not the S021.1 squeeze preset above) --
# tested 2026-09-28 at Anton's explicit request ("проверь при достижении 0.5, 1, и 1.5 РР") and
# REJECTED at all three levels (breakeven_at_r field comment above has the full numbers; also
# backtest-log.md 2026-09-28 and backtest/run_s021_breakeven.py, the committed reproducible
# sweep). ORB_BASE itself is untouched; each preset only sets breakeven_at_r via .with_().
ORB_BE05 = ORB_BASE.with_(breakeven_at_r=0.5)   # move stop to entry once +0.5R favorable
ORB_BE10 = ORB_BASE.with_(breakeven_at_r=1.0)   # move stop to entry once +1.0R favorable
ORB_BE15 = ORB_BASE.with_(breakeven_at_r=1.5)   # move stop to entry once +1.5R favorable
