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

    # --- costs (Gate 2). Applied per position in R space:
    #     cost_points = 2*spread_per_side + commission_points (round-trip);
    #     net_R = gross_R - cost_points / risk_points. Both 0.0 == gross (parity).
    #     Small-risk positions are hit harder by fixed-point costs (realistic). ---
    spread_per_side: float = 0.0     # points per side (round-trip = 2x)
    commission_points: float = 0.0   # round-trip commission, in index points

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
