"""Frozen strategy config for S021 -- ORB (opening range breakout) / intraday momentum.

Rules are frozen per claude/strategy-passport-S021.md sec 3 (Anton, 2026-09-21). This is
an independent from-scratch implementation, built by a Cowork session with no access to
Anton's original .../Trading/strategies/ORB-intraday-momentum/ code -- see passport sec 0
for why independent verification, not a review of the original, is the point of this
package. Do not tune these values here; a variant is a new preset via .with_(...)
(strategy-modifiers discipline) -- ORB_BASE itself must stay exactly what the passport
freezes.

session_open/session_close/entry_cutoff are clock readings against histdata's native
timestamps, which are TRUE New York local time (DST-aware), so 09:30 is the real cash
open all year round. A 2026-09-21 investigation concluded the opposite -- that the
timestamps were a fixed UTC-5 clock and the seasonal drift was deliberate -- and that
conclusion was WRONG; see engine.py's module docstring for the 2026-10-05 correction
(ALGODEV-61) and the two independent proofs. The values below are unchanged by that
correction: the backtest always anchored on the true open. What changed is the live
bots, which had been converting broker UTC into a fixed UTC-5 clock and so ran an hour
late every US-DST month.

S021.1 -- NAS100 Opening-Range Squeeze (claude/prompts-new-strategy-candidates.md P14,
"S021.1"; own reformalization of an idea from an external review (Google Gemini), see the
prompt's preamble). This is a PRESET tracked on this SAME engine -- a new tracking number,
NOT a new strategy/file -- per the 2026-09-24 decisions-log classification. The
squeeze_preset_enabled=False fields below are additive and unused by ORB_BASE/simulate();
NAS_SQUEEZE_PRESET below sets squeeze_preset_enabled=True and is read only by
engine.simulate_squeeze_preset(), a separate sibling function -- see that function's
docstring for why this preset needed its own function rather than a branch inside
simulate(). Session times for the preset are TRUE UTC (unlike ORB_BASE's New York clock
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
    session_open: time = time(9, 30)        # New York local clock reading -- see module docstring
    session_close: time = time(15, 59)
    entry_cutoff: time = time(14, 29)       # last minute of the entry scan window
    min_session_bars: int = 350             # day invalid below this many M1 bars in-session
    max_gap_days_per_session: float = 2.5   # skip day if the ADR lookback window is this stretched
    cost_bps_roundtrip: float = 1.0         # round-trip cost, bps of entry price, charged once/trade

    # --- S021.1 "NAS100 Opening-Range Squeeze" preset-only fields (default-off; unused by
    # the base ADR-band simulate()/ORB_BASE path above -- see engine.simulate_squeeze_preset()
    # and NAS_SQUEEZE_PRESET below). Session times here are TRUE UTC, not the New York clock
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

    # --- reversal modifier (default-off; unused by ORB_BASE). Anton asked 2026-09-28 for a
    # backtest of "reversals" on S021 and which reversal variants exist. None (the default)
    # reproduces the frozen ORB_BASE path byte for byte -- see backtest/run_s021_reversal.py's
    # regression check. Modes (REV_* constants below):
    #   "fade"          -- trade AGAINST the breakout: short at U / long at L, same 0.75*ADR14
    #                      stop, same 15:59 time exit, still max 1 trade/day. Sanity-check mode
    #                      (is the breakout edge real, or is the opposite side better?).
    #   "sar"           -- stop-and-reverse: base trade unchanged; if its ORIGINAL stop is hit at
    #                      or before reversal_cutoff, open ONE opposite leg at the stop price with
    #                      its own 0.75*ADR14 stop, exit 15:59 or stop. Max 2 legs/day.
    #   "flip_opposite" -- if the base trade reaches the OPPOSITE band (L for a long, U for a
    #                      short) at or before reversal_cutoff, close there and open ONE opposite
    #                      leg at that band (0.75*ADR14 stop, 15:59 exit). Max 2 legs/day.
    #   "flip_open"     -- same, but the flip level is the session open O (failed-breakout fade:
    #                      price returned all the way back to the open).
    # Flip levels always sit between entry and the original stop, so the flip is checked before
    # the stop on each bar (price must cross the flip level first). The new leg's own stop IS
    # checked on the flip bar itself (conservative, same convention as the base entry bar).
    # Not combinable with breakeven_at_r (engine raises ValueError).
    #
    # TESTED 2026-09-28, ALL FOUR REJECTED -- backtest/run_s021_reversal.py, reports/s021_reversal_*.csv.
    # Real 1bp cost, 2019-2026, 1518 signal days, R = net_pts / (0.75*ADR14), day R = sum of legs:
    #   BASE       +0.0713 R/day  sum +108.3R  maxDD -17.1R  8/8 yrs  solo Gate3 @0.5%: 90.6% cashout
    #   FADE       -0.0774        sum -117.5R  maxDD -119.6R 0/8 yrs  0.9% cashout (mirror of the edge)
    #   SAR        +0.0696        sum +105.6R  maxDD -20.8R  8/8 yrs  88.6%; reversal leg (n=217) -0.012R
    #   FLIP@band  +0.0413        sum  +62.6R  maxDD -20.9R  8/8 yrs  71.2%; leg n=605 -0.027R, and the
    #              early exit at L cuts the primary leg +0.071 -> +0.052R
    #   FLIP@open  -0.0219        sum  -33.2R  maxDD -38.2R  2/8 yrs  15.8%; leg n=989 -0.061R
    # At 3bp every variant degrades further (SAR 7/8 yrs, FLIP@band 6/8). A 15:29 reversal cutoff
    # changes nothing material. Root cause: after a failed breakout the market does NOT reliably
    # run the other way -- every reversal leg has negative expectancy -- and flips additionally
    # cut primary trades that would have recovered. SAR also doubles the worst day (-1.0 -> -2.0R).
    # Combo Gate 3 (S007 live + S021 variant) is ~99.7-100% cashout for every variant incl. FADE,
    # i.e. dominated by S007 and uninformative for choosing an S021 variant.
    reversal_mode: str | None = None
    reversal_cutoff: time | None = None     # last minute a reversal leg may open; None -> entry_cutoff

    # --- stop-variant modifiers (default-off; unused by ORB_BASE). Anton asked 2026-09-28 to
    # test different stop variants on the base version. Stop SIZE is the existing
    # stop_adr_mult (0.2 == stop at the session open O, 0.4 == stop at the opposite band, since
    # entry sits exactly on a band in the backtest; math.inf == no stop, time exit only).
    #   trail_adr_mult     -- trailing stop at this many ADR14 behind the best price since entry,
    #                         ratcheted after each bar's stop check (applies from the next bar);
    #                         the initial stop_adr_mult stop stays as the floor.
    #   stop_on_close      -- stop fires only when an M1 bar CLOSES beyond the stop, filled at
    #                         that close (ignores wicks; fill can be worse than the stop).
    #   time_stop_minutes  -- one check at entry + N minutes: exit at that bar's close if the
    #                         trade is not in profit.
    #
    # TESTED 2026-09-28 -- NO STOP VARIANT BEATS ORB_BASE's 0.75*ADR14 fixed stop; base kept.
    # backtest/run_s021_stops.py, reports/s021_stops_*.csv. 1bp, 2019-2026, n=1518, R vs each
    # variant's OWN initial risk (fixed-%-risk sizing):
    #   size 0.20 (=O)   +0.065R  DD -51.5R  5/8 yrs, negative at 3bp       -> reject
    #   size 0.30        +0.114R  DD -29.3R  8/8, losing streak 17
    #   size 0.40 (=L/U) +0.095R  DD -29.7R  8/8
    #   size 0.50        +0.086R  DD -27.6R  8/8
    #   BASE 0.75        +0.071R  DD -17.1R  8/8, streak 8
    #   size 1.00/1.50/2.00  +0.050/+0.029/+0.024R, DD -12.8/-9.4/-5.7R
    #   no stop          +0.063R  worst trade -5.6R (nominal 0.75*ADR sizing)
    #   trail 0.5/0.75/1.0   +0.051/+0.062/+0.070R
    #   close-stop 0.75/0.50 +0.071/+0.092R, worst trade -1.48/-1.64R
    #   time-stop 30/60/120m +0.039/+0.047/+0.050R
    # Tighter stops raise R/trade but raise DD-in-R about as much: return/maxDD is ~0.0042 for
    # BASE vs 0.0031-0.0039 for 0.3-0.5 stops. The fair test is Gate 3 with risk re-optimised per
    # variant (reports/s021_stops_gate3_riskgrid.csv): best cashout BASE 90.6% @0.50%, trail0.5
    # 90.7% @0.75%, close-stop0.5 90.4% @0.50%, size0.5 89.8% @0.50%, size0.3 88.3% @0.35%,
    # size1.0 88.3% @0.75% -- all within ~3pp, i.e. the stop only rescales the same edge. Tight
    # stops are additionally the most cost/slippage-fragile (size 0.30 at 3bp: +0.114 -> +0.065R;
    # live entry is the close of the breakout minute, not the band), so no promotion.
    trail_adr_mult: float | None = None
    stop_on_close: bool = False
    time_stop_minutes: int | None = None

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

# Reversal-mode names (config.reversal_mode values) -- see the reversal_mode field comment.
REV_FADE = "fade"
REV_SAR = "sar"
REV_FLIP_OPPOSITE = "flip_opposite"
REV_FLIP_OPEN = "flip_open"
REVERSAL_MODES = (REV_FADE, REV_SAR, REV_FLIP_OPPOSITE, REV_FLIP_OPEN)

# Reversal presets on the BASE ORB_BASE path -- requested 2026-09-28 (Anton: "сделай бектест
# разворотов для 021") and REJECTED -- all four; numbers in the reversal_mode field comment above.
ORB_REV_FADE = ORB_BASE.with_(reversal_mode=REV_FADE)
ORB_REV_SAR = ORB_BASE.with_(reversal_mode=REV_SAR)
ORB_REV_FLIP_OPP = ORB_BASE.with_(reversal_mode=REV_FLIP_OPPOSITE)
ORB_REV_FLIP_OPEN = ORB_BASE.with_(reversal_mode=REV_FLIP_OPEN)
