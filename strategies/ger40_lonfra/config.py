"""Strategy configuration for S007 (GER40 London x Frankfurt, pyramiding).

All tunables live here so the engine stays a pure state machine. The frozen
baseline the user selected is ``BASELINE_S007`` (wide common 0.5 stop +
liquidity take-profit + hold to 16:59). Other presets exist purely so the
walk-forward can compare the baseline against the "honest" mechanical core and
the robust A-only subset on equal footing.

Two presets are deployment-relevant rather than research-only:
``WORKING_S007_LIQFLOOR`` is what the live demo bot runs (see
bot/s007_config.py::PRESET), and ``WORKING_S007_NEWSSAFE`` is the prop-firm
candidate that closes the day before the scheduled macro-release block instead
of at 16:59. Neither is selected automatically -- bot/s007_config.py names one.

Times are HH:MM strings in Kyiv time (the raw data is already Kyiv, see
data/README_data_conventions.md); comparisons are lexical on the 'HH:MM'
string exactly as in the reference scripts, which is why the fields are strings.
"""
from __future__ import annotations

from dataclasses import dataclass, replace


@dataclass(frozen=True)
class StrategyConfig:
    # --- structure / pyramiding ---
    k: int = 2                       # swing-fractal half-window (CHoCH sensitivity)
    max_positions: int = 4           # cap on total positions per day (1 = no adds)
    do_pyramid: bool = True          # add on each CHoCH in trend direction
    # minimum size of the broken swing for a valid add ("meaningful structure"),
    # as absolute points and/or a fraction of the Frankfurt range height. This
    # validates an add as a real CHoCH, not a micro-swing. 0 == no filter.
    min_swing_points: float = 0.0
    min_swing_frac: float = 0.0
    add_window_end: str | None = None  # no NEW adds after this HH:MM (None = whole window)

    # alternative risk model: unlimited adds, but the day is stopped when the
    # aggregate (realized + open, marked-to-market) loss reaches daily_loss_cap_R.
    # daily_loss_cap_R is in R; at 1R=0.5% a 2% cap = 4R, at 0.25% = 8R.
    # TESTED 2026-07-22 and REJECTED as-is: this only bounds the LOSING side of
    # the day -- on a winning trend day eff_max becomes 10**9 with nothing else
    # capping add count, so net R/day grows ~monotonically and unrealistically
    # with the cap (Dukascopy net: +0.437R at cap=4 (=baseline, unreached) up to
    # +2.80R at cap=16, with drawdown NOT scaling proportionally) -- a leverage/
    # concentration artifact (real broker margin/liquidity would stop this long
    # before 10**9 positions), not a discovered edge. Needs a companion cap on
    # add COUNT (not just loss) before this is usable. See experiments-log.md S007 E2.
    unlimited_adds: bool = False
    daily_loss_cap_R: float | None = None

    # --- stop ---
    # 'mid_range'  : wide common non-trailing stop (B->mid 0.5, A->opposite bound)
    # 'last_swing' : per-position stop behind last confirmed swing (trails)
    # 'prev_swing' : behind the previous swing (a bit wider)
    # 'prev_fvg'   : behind last opposite 1M FVG zone (wider)
    stop_mode: str = "mid_range"

    # --- take profit ---
    # 'range'      : fixed 100% of range (B) / opposite boundary (A)
    # 'liquidity'  : nearest liquidity proxy (asia / prior-day / prev swing)
    tp_mode: str = "liquidity"

    # tp_mode="liquidity" only: floor the liquidity candidate search at the
    # standard 100%-range target (range_tp) instead of just "beyond the broken
    # boundary". Without this, liquidity_tp() can pick a level much closer than
    # range_tp, producing near-zero-R trades that resolve in 1-2 minutes -- too
    # fast for the live bot's 1-minute poll to reliably catch (see ALGODEV-21,
    # real example 2026-08-11: tp picked 0.7pt from entry vs. range_tp 51.5pt
    # away). ON: use range_tp as the floor -- a candidate nearer than range_tp
    # is discarded, so tp is range_tp unless liquidity lies BEYOND it. Off by
    # default (base untouched, ALGODEV-21 fix opt-in per strategy-modifiers).
    liquidity_tp_floor: bool = False

    # tp_mode="liquidity" only: cap the liquidity candidate at
    # range_tp + this many points (None = uncapped, the ALGODEV-21 behavior).
    # The floor above only prevents a target NEARER than range_tp; nothing
    # previously prevented one much FARTHER -- found live 2026-09-02 (Anton):
    # a real cycle picked liquidity 285.9pt beyond range_tp (TP 26235.1 vs
    # range_tp 25949.2), a move unlikely to complete inside one session.
    # UNTESTED as of this writing -- ALGODEV-35 experiment, see
    # backtest/run_s007_liqceiling.py for the points-swept comparison
    # (10/20/30/40/50/100) against WORKING_S007_LIQFLOOR (uncapped). Only
    # meaningful when liquidity_tp_floor=True (an uncapped-and-unfloored
    # combination reintroduces the near-zero-R problem the floor fixed).
    liquidity_tp_ceiling_points: float | None = None

    # --- session windows (Kyiv) ---
    fr_start: str = "09:00"          # Frankfurt (Xetra) range start
    fr_end: str = "09:59"            # Frankfurt range end
    trade_start: str = "10:00"       # London open / start of trade search
    exit_end: str = "16:59"          # last bar; open positions marked out here

    # --- setups enabled ---
    allow_A: bool = True             # 0.5 mid-break setup
    allow_B: bool = True             # boundary breakout setup

    # --- pre-entry day filters (raise net profitability / cut drawdown) ---
    max_height: float | None = None  # skip day if Frankfurt height > this (points)
    # TESTED 2026-07-22 and REJECTED as a day-quality filter: sweeping
    # min_height in {5..40} on BASELINE_S007/WORKING_S007 (net, real spread,
    # Dukascopy 2023-2026) nudges the aggregate net R/day up slightly at low
    # thresholds (10-15pt: +0.437->+0.446..0.453R) but at higher thresholds
    # (25-40pt) individual years collapse toward flat/negative (e.g. 2023/2024
    # go near-zero or negative) while the aggregate average keeps climbing --
    # the same overfitting signature as the already-rejected DOW/entry-time
    # filters (backtest-log.md), not a robust edge. See experiments-log.md S007 E1.
    min_height: float | None = None
    max_gap_points: float | None = None  # skip if |10:00 open - prior RTH close| > this
    # scenario A only: skip if the entry (confirmation) candle already reaches the
    # target boundary (up -> Frankfurt HIGH, down -> LOW).
    # TESTED 2026-07-16 and REJECTED: those 37 A-days average +1.28R (78% win) —
    # a candle that tags the boundary is a momentum tell, not exhaustion. Enabling
    # this cuts A expectancy (+0.53->+0.41R), worst-year (+0.19->+0.06) and deepens
    # drawdown (-40->-49R). Kept as a documented off-by-default option.
    skip_A_entry_reaches_boundary: bool = False

    # B-reversal -> A model: when a B breakout FAILS and price returns to 0.5, flip
    # the day into a scenario-A trade from the midline toward the opposite boundary
    # (wide common stop + pyramiding). Recovers losing-B days. Off = base untouched.
    b_reversal_to_A: bool = False

    # cap on TOTAL positions in the recovery leg only (first entry + adds); None =
    # base behavior (recovery leg uses max_positions like the primary leg).
    # Motivation (ALGODEV-14 worst-day study, 2026-08-31): every one of the 30
    # worst days (<= -5R) on WORKING_S007_LIQFLOOR is a B "double failure" --
    # primary B leg fully stopped (4 pos), then the recovery leg fully stopped
    # too (4 more). The recovery leg is net positive overall (+91.5R across 187
    # legs, 70% win) so disabling it loses money; capping its adds trims only
    # the second half of the double failure. Counterfactual at cap=2:
    # sum +544.5 -> +508.0R (-6.7%), worst day -9.03 -> -6.75R (-25%),
    # maxDD -50.9 -> -47.8R. A worst-day trade-off aimed at prop daily limits,
    # not an expectancy improvement.
    # TESTED 2026-08-31 (backtest/run_s007_tailrisk.py, real engine run, both
    # working presets, spread 0.635/side) and ACCEPTED for the prop axis only:
    # cap=2 gives up 6.1-6.7% of profit for -25% worst day -- more worst-day
    # reduction per unit of profit than uniform size-scaling can buy -- while
    # maxDD stays ~unchanged (drawdown comes in multi-day streaks, not one day).
    # On WORKING_S007_NEWSSAFE all years stay positive (2023 +30.5 -> +24.6R);
    # on WORKING_S007_LIQFLOOR 2023 dips to -0.8R, so the cap is recommended
    # only stacked on NEWSSAFE (see WORKING_S007_PROP below). Not for the live
    # demo preset: on the pure-expectancy axis it is a straight loss.
    max_recovery_positions: int | None = None

    # --- risk sizing on adds (does not change R of a single position; scales the
    #     contribution of pyramided positions to the daily R sum). 1.0 == parity
    #     with the reference scripts. <1.0 tames drawdown, per pyramid_findings. ---
    risk_per_add: float = 1.0

    # --- costs (Gate 2). Applied per position in R space, two selectable models:
    #     'points' (legacy, default): cost_points = 2*spread_per_side + commission_points,
    #       a FIXED number of index points for every position regardless of price level.
    #     'bps': cost_points = entry_price * (2*spread_bps_per_side + commission_bps) / 10000,
    #       i.e. cost scales with the position's own entry price.
    #     net_R = gross_R - cost_points / risk_points either way. All 0.0 == gross (parity).
    #     Small-risk positions are hit harder by cost (realistic in both models).
    #
    #     Why 'bps' exists (ALGODEV-21 follow-up, 2026-08-12): the real broker spread
    #     (REAL_SPREAD_PER_SIDE=0.635pt in backtest/run_s007_*.py) was measured from
    #     recent (2026) MT5 data, when GER40 traded around ~24-25k. Applied as a FIXED
    #     point value across the whole 2023-2026 backtest window, it overstates cost in
    #     the earlier years: GER40 traded around ~15.8k in 2023 (avg entry price), so the
    #     same 1.27pt round-trip consumed ~5.8% of average risk in 2023 vs only ~3.2% in
    #     2026 (see decisions-log.md 2026-08-12) -- a purely mechanical artifact of the
    #     index's ~57% nominal price growth over the window, not a real difference in
    #     year-to-year trading cost. 'bps' removes that artifact by keeping cost a
    #     constant fraction of each trade's own entry price instead of a fixed point
    #     amount, so year-over-year comparisons in this backtest aren't systematically
    #     tilted toward the years when the index happened to be more expensive. ---
    cost_model: str = "points"       # 'points' (legacy/default) or 'bps'
    spread_per_side: float = 0.0     # points per side (round-trip = 2x); 'points' model
    commission_points: float = 0.0   # round-trip commission, in index points; 'points' model
    spread_bps_per_side: float = 0.0  # basis points of entry price, per side; 'bps' model
    commission_bps: float = 0.0       # round-trip commission, in bps of entry price; 'bps' model

    # --- minimum risk guard (kills the near-zero-risk R-explosion artifact:
    #     an add whose entry sits ~on the common 0.5 stop yields absurd R).
    #     Effective buffer = max(min_risk_points, min_risk_frac * range_height).
    #     Both 0.0 == reference parity (no guard). ---
    min_risk_points: float = 0.0
    min_risk_frac: float = 0.0

    # --- data hygiene (matches reference run() guards) ---
    min_fr_bars: int = 45            # require a reasonably complete Frankfurt hour
    min_ld_bars: int = 60            # require enough London bars

    def with_(self, **kw) -> "StrategyConfig":
        return replace(self, **kw)


# The frozen baseline chosen by the user: full stack, +0.588R on Dukascopy.
# Includes the min-risk hygiene guard (frac 0.10 of range height, floor 2 pts) that
# removes the near-zero-risk R-explosion artifact found in validation. The guard is
# a bugfix, not a rule change: it barely moves the median but kills fake outliers.
BASELINE_S007 = StrategyConfig(
    k=2, max_positions=4, do_pyramid=True,
    stop_mode="mid_range", tp_mode="liquidity",
    trade_start="10:00", exit_end="16:59",
    allow_A=True, allow_B=True,
    min_risk_frac=0.10, min_risk_points=2.0,
)

# Honest mechanical core: wide common stop + fixed 100% TP, 2h window (+0.409R).
HONEST_CORE = BASELINE_S007.with_(tp_mode="range", exit_end="11:59")

# Most robust subset per user's own findings: A only, narrow Frankfurt range.
A_ONLY_NARROW = HONEST_CORE.with_(allow_B=False, max_height=36.0)

# Baseline + robust day filters found in filter analysis (2026-07-16): cap the
# widest Frankfurt ranges (best expectancy filter, all years consistent) and,
# optionally, extreme overnight gaps (drawdown reducer). Round absolute
# thresholds (not exact quantiles) to avoid over-fitting the sample.
FILTERED_S007 = BASELINE_S007.with_(max_height=100.0)                     # +exp, -DD
FILTERED_S007_TIGHT = BASELINE_S007.with_(max_height=100.0, max_gap_points=200.0)  # min DD

# B-reversal->A validated (2026-07-16): failed B that returns to 0.5 flips into an
# A-model trade to the opposite boundary. Net +0.415->+0.531R at real spread, all
# years up, worst-year +0.19->+0.27, robust to spread, no look-ahead.
REVERSAL_S007 = BASELINE_S007.with_(b_reversal_to_A=True)
# Recommended working config: height filter + reversal (highest net, all years +).
WORKING_S007 = BASELINE_S007.with_(max_height=100.0, b_reversal_to_A=True)
# + "meaningful CHoCH" add validation (broken swing >= 25% of range height):
# validates adds as real structure breaks, not micro-swings; trades a little
# expectancy for much lower drawdown (-40->-26R) and best worst-year (+0.35).
# Preferred for prop sizing. (min_swing_points=10 instead maximizes expectancy.)
WORKING_S007_V2 = WORKING_S007.with_(min_swing_frac=0.25)

# ALGODEV-21 fix: WORKING_S007 + liquidity TP floored at range_tp, so
# tp_mode="liquidity" can no longer pick a target closer than the standard
# 100%-range projection. VALIDATED (Gate 1/Gate 2) and PROMOTED to
# bot/s007_config.py::PRESET on 2026-08-12 by Anton's explicit decision, taken
# despite a mixed result (higher net, lower win rate, deeper maxDD, weaker 2023)
# -- see backtest-log.md 2026-08-11/12 and decisions-log.md.
WORKING_S007_LIQFLOOR = WORKING_S007.with_(liquidity_tp_floor=True)

# ALGODEV-35 experiment (2026-09-02, Anton): does capping how far BEYOND
# range_tp a liquidity target may sit help or hurt? WORKING_S007_LIQFLOOR only
# floors (never nearer than range_tp); these sweep a ceiling on top of that
# floor at several point values via backtest/run_s007_liqceiling.py.
# TESTED 2026-09-02 and REJECTED, all six: Gate 2 (real spread) net R/day for
# ceil10/20/30/40/50/100 = +0.676/+0.687/+0.729/+0.784/+0.833/+0.837R vs
# uncapped LIQFLOOR's +0.801R -- every ceiling <=40pt is worse (lower net,
# usually deeper maxDD, e.g. ceil20/30 hit -65R vs LIQFLOOR's -50.9R) despite
# a higher day win-rate; only ceil50/100 approach uncapped, and ceil100's
# "improvement" (+0.837 vs +0.801, maxDD identical at -50.9R) is really "the
# cap almost never binds", not a real edge. Distant liquidity targets that
# look alarming on any single day (the 2026-09-02 case that prompted this:
# TP 285.9pt beyond range_tp) are net CONTRIBUTORS to the 3+-year edge, not
# noise -- capping them tighter loses profitability. Base (WORKING_S007_
# LIQFLOOR, uncapped) stays the live preset; none of these promoted.
WORKING_S007_LIQCEIL10 = WORKING_S007_LIQFLOOR.with_(liquidity_tp_ceiling_points=10.0)
WORKING_S007_LIQCEIL20 = WORKING_S007_LIQFLOOR.with_(liquidity_tp_ceiling_points=20.0)
WORKING_S007_LIQCEIL30 = WORKING_S007_LIQFLOOR.with_(liquidity_tp_ceiling_points=30.0)
WORKING_S007_LIQCEIL40 = WORKING_S007_LIQFLOOR.with_(liquidity_tp_ceiling_points=40.0)
WORKING_S007_LIQCEIL50 = WORKING_S007_LIQFLOOR.with_(liquidity_tp_ceiling_points=50.0)
WORKING_S007_LIQCEIL100 = WORKING_S007_LIQFLOOR.with_(liquidity_tp_ceiling_points=100.0)

# Scheduled-news cutoff for the prop deployment. The afternoon block of the Kyiv
# session carries the day's worst tail risk from scheduled macro releases: a
# per-minute M1 range study over 2023-06..2026-08 puts the 15:30 bar (US 8:30 ET
# -- CPI/NFP/PPI) at a p99 of 50.6 bps against a ~3 bps all-day median, i.e. a
# normally quiet minute that occasionally explodes ~16x, with the ECB decision at
# 15:15 just ahead of it. 14:29 clears both; NEWS_SAFE_EXIT_END takes a further
# 5-minute buffer because a prop firm's server clock can differ from ours and
# flattening up to 8 pyramided positions over the cTrader API is not instant
# (Anton's call, chat 2026-08-21).
NEWS_SAFE_EXIT_END = "14:24"

# PROP deployment candidate (2026-08-21): WORKING_S007_LIQFLOOR flattened before
# the scheduled-news block instead of at 16:59.
# TESTED 2026-08-21 and ACCEPTED on both cost models, 2023-06-26..2026-08-11:
#   bps costs: net +0.8498 -> +0.9161 R/day, sum +577.9 -> +622.9R, win 60 -> 62%,
#              maxDD -47.5 -> -48.2R, worst day -8.69R (UNCHANGED), and the
#              weakest year gains the most: 2023 +18.0 -> +43.9R.
#   pts costs: net +0.8008 -> +0.8665 R/day, 2023 +4.9 -> +30.5R.
#   gross    : net +1.0337 -> +1.1017 R/day, maxDD -40.8 -> -38.3R.
# Only 2025 gives ground (312.5 -> 275.8R); 3 of 4 years improve. The afternoon
# tail being given up is 3% of all profit (+18.1R of +577.9R) and is close to
# noise (63 days better / 63 days worse), while on the 12 exposed days whose
# 15:30 bar exceeded 20 bps the average day was -1.69R against +1.49R otherwise.
# METHOD NOTE: exit_end is a PLATEAU here, not a peak -- 14:29..15:29 all land
# within 97-105% of the 16:59 result -- so the cutoff is picked on an a-priori
# risk ground (a known, scheduled release time), not by choosing the best number.
# That is what separates this from the min_height variant rejected 2026-07-22.
# CAVEAT: the engine fills stops AT the stop price and models no slippage at all,
# so the news protection this buys is strictly larger than the backtest can show
# -- and, symmetrically, the -8.69R worst day is a lower bound, not the real one.
# NOT a sizing fix: at RISK_PCT=0.25% the historical maxDD is ~12% of the account
# and would breach a 10% prop max-loss limit with or without this preset. See
# decisions-log.md 2026-08-21.
WORKING_S007_NEWSSAFE = WORKING_S007_LIQFLOOR.with_(exit_end=NEWS_SAFE_EXIT_END)

# PROP candidate (2026-08-31, ALGODEV-14 follow-up): NEWSSAFE + recovery leg
# capped at 2 positions. Targets the prop DAILY loss limit specifically: every
# one of the 30 worst days is a B double failure whose second half the cap
# removes. Numbers (2023-06-26..2026-08-11, net at 0.635pt/side):
#   NEWSSAFE          net +0.8665 sum +589.2R maxDD -53.5R worst -9.03R
#   NEWSSAFE + cap=2  net +0.8139 sum +553.5R maxDD -54.0R worst -6.75R
# i.e. -6.1% profit for -25% worst day; all years positive. At 0.25%/R the
# worst day drops from ~2.26% to ~1.69% of the account. maxDD is NOT improved
# -- this preset does not fix the max-total-drawdown constraint, only the
# daily one. NOT promoted to any bot config; candidate pending Anton's call.
WORKING_S007_PROP = WORKING_S007_NEWSSAFE.with_(max_recovery_positions=2)

# --- Exact reproductions of the two reference result files (regression only) ---
# pyramid_duka.csv  <- pyramid.py run(k=2,max=4, use_structure_stop=True), 2h, range TP
REF_PYRAMID_DUKA = StrategyConfig(
    k=2, max_positions=4, do_pyramid=True,
    stop_mode="last_swing", tp_mode="range",
    trade_start="10:00", exit_end="11:59",
    allow_A=True, allow_B=True,
)
# pyramid_liq_duka.csv <- pyramid_v2.py stop=mid_range, tp=liquidity, exit 16:59
# (guard-free, so it reproduces the historical file exactly).
REF_PYRAMID_LIQ_DUKA = StrategyConfig(
    k=2, max_positions=4, do_pyramid=True,
    stop_mode="mid_range", tp_mode="liquidity",
    trade_start="10:00", exit_end="16:59",
    allow_A=True, allow_B=True,
)
