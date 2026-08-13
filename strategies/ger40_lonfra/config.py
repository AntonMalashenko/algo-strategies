"""Strategy configuration for S007 (GER40 London x Frankfurt, pyramiding).

All tunables live here so the engine stays a pure state machine. The frozen
baseline the user selected is ``BASELINE_S007`` (wide common 0.5 stop +
liquidity take-profit + hold to 16:59). Other presets exist purely so the
walk-forward can compare the baseline against the "honest" mechanical core and
the robust A-only subset on equal footing.

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

# ALGODEV-21 fix candidate: WORKING_S007 + liquidity TP floored at range_tp, so
# tp_mode="liquidity" can no longer pick a target closer than the standard
# 100%-range projection. Backtest (Gate 1/Gate 2) NOT YET RUN -- do not promote
# to bot/s007_config.py::PRESET until validated. See decisions-log.md.
WORKING_S007_LIQFLOOR = WORKING_S007.with_(liquidity_tp_floor=True)

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
