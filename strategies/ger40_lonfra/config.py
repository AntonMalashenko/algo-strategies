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

    # move a position's own stop to its own entry (breakeven) once price has
    # moved cfg.breakeven_at_r * that position's OWN risk (|entry-stop0| at
    # creation) in its favor. None (default) = OFF, base untouched -- BE was
    # explicitly excluded from S007's design from the start (strategy-passport
    # S007 sec 2, 2026-07-16 decision: "BE net; davlenie ubrano"). Applied
    # per-position (primary entry AND every add each use their own entry/risk),
    # so once armed, positions in the same leg can end up with DIFFERENT stop
    # prices -- this breaks the "one common stop_mode=mid_range stop for the
    # whole leg" property that is core to S007's documented edge (strategy-
    # spec-S007.md sec 4: "ODIN OBSHCHIY stop"). Checked AFTER the existing
    # stop/tp test within a bar (same conservative "stop before target"
    # convention the rest of this engine uses), so a bar that hits the
    # ORIGINAL stop before reaching the BE trigger still stops out at the
    # original level -- the move only affects FUTURE bars. R-normalization
    # always uses the position's risk0 (the risk at creation), never the
    # possibly-moved current stop, so a position that reaches breakeven and
    # later hits TP still scores its full R relative to its original risk,
    # not a corrupted near-zero denominator.
    #
    # TESTED 2026-09-02 (Anton, "БУ после 1R") and REJECTED at breakeven_at_r
    # =1.0, the value asked for -- backtest/run_s007_breakeven.py, Dukascopy
    # 2023-06-26..2026-08-11, real spread 0.635/side:
    #   BASELINE_S007:            net +0.4488 -> +0.4317 R/day (-3.8%), maxDD
    #                              -41.2 -> -32.7R, worst-year +21.1 -> +33.5
    #   WORKING_S007_LIQFLOOR
    #   (current live preset):    net +0.8008 -> +0.6808 R/day (-15.0%),
    #                              maxDD -50.9 -> -49.4R, worst-year
    #                              +4.9 -> +28.2
    # Costs expectancy on both bases, worse on the live preset specifically
    # (it pyramids harder, so BE clips more of the run that IS the edge --
    # strategy-spec-S007.md sec 10.1: "profit -- v doborah"). Consistent with
    # every previously-tested per-position/individual-stop variant losing to
    # the common stop (sec 11: "vse huzhe obshchego stopa"), since arming BE
    # is exactly a partial move toward individual per-position stops.
    # Robustness sweep (0.5/1.0/1.5/2.0 R) shows a real, non-monotonic
    # trade-off, not just noise around 1R: 0.5R is worse still (-25.5% on the
    # live preset); by 1.5-2.0R the expectancy hit shrinks and even turns
    # slightly positive on BASELINE_S007 (+1.8%/+2.9%) while staying negative
    # on the live preset (-2.0%/+3.0% -- 2.0R is a rare near-breakeven/better
    # case). worst-year improves at EVERY tested level on both bases (BE
    # protects exactly the B-double-failure tail already described in
    # max_recovery_positions' comment above) while maxDD is roughly flat to
    # slightly better. Net verdict at r=1.0 (what was asked): REJECTED -- a
    # real expectancy cost on the config that matters (the live preset), not
    # a lucky/unlucky single number. NOT closed off entirely: the worst-year/
    # tail-risk improvement at every level, and near-breakeven expectancy at
    # 1.5-2.0R on BASELINE_S007, could be worth a dedicated prop-sizing
    # follow-up (stacked with WORKING_S007_PROP's max_recovery_positions cap,
    # which targets the same double-failure tail from the add-count side
    # instead of the stop side) -- not done in this pass. See
    # experiments-log.md S007 E4, backtest-log.md 2026-09-02.
    breakeven_at_r: float | None = None

    # breakeven_offset_points (ALGODEV-41, Anton 2026-09-10: "make the BE a few
    # points more favourable so even tiny losses don't happen"): when the
    # breakeven rule above fires, the stop goes to entry + this many points
    # (long) / entry - this many points (short) instead of exactly `entry`, so a
    # subsequent retrace onto the BE stop exits `breakeven_offset_points` in
    # PROFIT -- enough to clear the ~1.27pt round-trip spread + commission
    # instead of booking it as a small loss (which is what an entry-exact BE
    # exit does today: gross 0.0R minus cost_points/risk_points).
    # Unit: RAW engine points, the same convention as max_height and
    # liquidity_tp_floor_points -- NOT R, and not a fraction of risk0.
    # Only meaningful when breakeven_at_r is not None (nothing reads it
    # otherwise). 0.0 (default) == today's exact-entry behaviour, base
    # untouched.
    # Constraint: must stay well below breakeven_at_r * risk0, or the "moved"
    # stop would sit at/beyond the trigger price that armed it and the position
    # would be stopped out on the next bar's first touch. engine.py clamps it to
    # trigger - BE_OFFSET_MIN_GAP_POINTS as a safety net, but a preset should
    # stay a few points against a ~30-60pt risk0, not anywhere near it.
    #
    # TESTED 2026-09-10 (backtest/run_s007_breakeven.py, Dukascopy
    # 2023-06-26..2026-08-11, offsets 0/1/2/3/5pt at the live
    # breakeven_at_r=0.5, Gate 1 + Gate 2 at 0.635/side). VERDICT: the stated
    # goal IS achieved -- BE exits stop being small losses from ~2pt up -- but
    # the whole-book effect is MARGINAL and NON-MONOTONIC, so nothing is
    # promoted. Gate 2 net R/day:
    #   WORKING_S007_NEWSSAFE_MAX8_BE05 (deployment candidate, max_positions=8)
    #     off=0.0 +1.6513   off=1.0 +1.6260 (-1.5%)   off=2.0 +1.6287 (-1.4%)
    #     off=3.0 +1.6623 (+0.7%)            off=5.0 +1.6217 (-1.8%)
    #   BASELINE_S007 + BE@0.5R
    #     off=0.0 +0.4406   off=1.0 +0.4275 (-3.0%)   off=2.0 +0.4333 (-1.7%)
    #     off=3.0 +0.4468 (+1.4%)            off=5.0 +0.4605 (+4.5%)
    # The BE-exit bucket (positions that actually exited ON the moved stop)
    # behaves exactly as intended and crosses zero between 1 and 2pt, i.e. at
    # ~the 1.27pt round-trip spread this is meant to clear:
    #   MAX8_BE05, n(BE-exits) 1269..1444 of 5049 positions, total / avg R:
    #     off=0.0  -87.5R / -0.0689   off=1.0  -20.9R / -0.0160
    #     off=2.0  +42.1R / +0.0314   off=3.0 +101.0R / +0.0737
    #     off=5.0 +208.7R / +0.1445
    #   BASELINE_S007, n 629..683 of 2313:
    #     off=0.0  -36.1R / -0.0575   off=2.0  +18.1R / +0.0278
    #     off=3.0  +43.7R / +0.0659   off=5.0  +90.7R / +0.1329
    # (At off=0.0 the same bucket is EXACTLY 0.00R gross and negative only
    # after costs -- the arithmetic proof of Anton's complaint.)
    # The tradeoff is the usual S007 one: the offset also arms a TIGHTER
    # post-breakeven stop on every be_moved position, so more positions exit
    # there at all (1269 -> 1444 on MAX8_BE05) and some would-be runners get
    # clipped. That clipping roughly cancels the bucket's gain, which is why
    # net R/day wobbles within +-2% instead of tracking the bucket. maxDD is
    # the one axis that improves consistently and monotonically with the
    # offset (MAX8_BE05 -19.2 -> -15.6R at 5pt; BASELINE -25.4 -> -23.5R), and
    # day win-rate rises (69 -> 72%), both for the same reason. Worst-year
    # goes the OTHER way on the deployment candidate (+173.9 -> +154.3R at
    # 3pt) while improving on BASELINE (+17.3 -> +25.1R at 5pt).
    # Best single value = 3.0pt: the only offset positive on BOTH bases
    # (+0.7% / +1.4% net), with maxDD better on both and the BE-exit bucket
    # solidly positive. But 5pt being WORSE than 3pt on the deployment
    # candidate while better on BASELINE means 3pt is within sampling noise,
    # not a located optimum -- treat it as "costs nothing, cleans up the
    # small-loss exits", not as an edge. Named as
    # WORKING_S007_NEWSSAFE_MAX8_BE05_OFF3 below, NOT promoted.
    # Gate 3 (prop cashout%/daily-bust%, backtest/run_s007_propscheme.py --
    # the axis BE@0.5R was actually chosen on) NOT re-run for the offset; see
    # that preset's comment below.
    #
    # LIVE (2026-09-11, Anton's explicit decision, shown this whole tradeoff):
    # 2.0pt (WORKING_S007_NEWSSAFE_MAX8_BE05_OFF2, see its own comment below)
    # is wired to the S007 demo account (account_strategy id=1) -- the
    # smallest swept offset whose BE-exit bucket is actually positive, chosen
    # over 3.0pt's slightly-better net R/day to keep the offset minimal.
    breakeven_offset_points: float = 0.0

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
    # prevents one much FARTHER -- found live 2026-09-02 (Anton): a real
    # cycle picked liquidity 285.9pt beyond range_tp (TP 26235.1 vs range_tp
    # 25949.2), a move unlikely to complete inside one session. Only
    # meaningful when liquidity_tp_floor=True (an uncapped-and-unfloored
    # combination reintroduces the near-zero-R problem the floor fixed).
    #
    # TESTED 2026-09-22 (backtest/run_s007_liqceiling.py, Dukascopy
    # 2023-2026, real spread 0.635/side, WORKING_S007_LIQFLOOR base) and
    # REJECTED as a fix for the "TP looks disconnected from any real level"
    # complaint it was meant to address (a live cycle that same day picked
    # prev_day_low as TP, 95.8pt beyond range_tp, from nothing more than the
    # single lowest M1 wick of the prior session -- not a confirmed
    # structural level). A tight-enough cap to have actually constrained
    # that case (10-40pt) costs real expectancy: net +0.80R/day (uncapped)
    # -> +0.68-0.78R/day, worse maxDD at every point in that range (up to
    # -65.6R vs -50.9R at 30pt). Only very loose caps (50-100pt) roughly
    # match uncapped (+0.83-0.84R/day) -- but those wouldn't have capped
    # today's 95.8pt case either, so they don't address the complaint.
    # Rare far-reaching targets apparently pay for themselves across the
    # full history (the R from the days they DO get hit outweighs the cost
    # of the days they don't -- an unreached target isn't a loss, the
    # position just resolves 'eod' at exit_end same as any other). Kept as
    # a documented off-by-default option; if the underlying complaint is
    # revisited, the more promising direction is tightening what counts as
    # a "liquidity" CANDIDATE in the first place (require a confirmed
    # structural swing, not the bare min/max of an arbitrary session
    # window) rather than capping distance after the fact.
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

    # Confirmation style for the b_reversal_to_A trigger above. False (default,
    # base untouched) arms the reversal on a bare intrabar low/high TOUCH of mid
    # (engine.py's original rule) and fills the simulated entry AT mid exactly,
    # regardless of where that bar closed. True requires the SAME close-based,
    # two-bar confirmation setups.py::find_setup already uses for the day's
    # primary A/B detection (a close beyond mid, then the NEXT close still
    # beyond it) before arming the reversal, and fills at that confirming
    # bar's close instead of an assumed mid fill.
    # MOTIVATION (found live 2026-09-16, ALGODEV-42 follow-up): the touch-only
    # rule armed a same-day recovery leg off a GER40 M1 bar whose low touched
    # mid to the tenth of a point (25478.9) and closed back at 25486.4 -- 7.5pt
    # above, near its own high -- the same minute. No real follow-through, yet
    # the engine (and, since bot/s007_signals.py::plan_now replays this exact
    # function live, the live bot too) treated it as an armed reversal. The
    # +91.5R/187-leg/70%-win recovery-leg verdict already documented on
    # max_recovery_positions above was measured with the touch-only rule, so it
    # already includes whatever share of these wick-only arms lost -- this flag
    # tests whether requiring an actual close-based confirmation, as the
    # primary setup already does, changes that verdict for better or worse.
    #
    # TESTED 2026-09-16 (backtest/run_s007_reversal_confirm.py, Dukascopy
    # 2023-2026, real spread 0.635/side) and REJECTED on the expectancy axis
    # across every base that enables b_reversal_to_A:
    #   REVERSAL_S007:                    net +0.5614 -> +0.5384R/day (-4.1%)
    #   WORKING_S007:                     net +0.5871 -> +0.5636R/day (-4.0%)
    #   WORKING_S007_NEWSSAFE_MAX8_BE05:  net +1.6513 -> +1.6042R/day (-2.9%)
    # Confirmation DOES do what it's supposed to: it raises the recovery leg's
    # own win rate substantially (53-67% -> 60-73%) and cuts the live preset's
    # maxDD slightly (-19.2 -> -18.9R) with a marginally better worst year
    # (+173.9 -> +175.7) -- it correctly filters out wick-only noise. But it
    # ALSO filters out enough genuinely profitable fast V-reversals (ones that
    # never produce two confirming closes before running to target) that the
    # net R given up exceeds what the filtered noise cost: on the live preset,
    # recovery-leg trade count drops 695->564 (-19%) and its total R drops
    # +121.4->+89.4R (-26%), more than the higher win rate recovers. Kept as a
    # documented off-by-default option -- not a fix, since the 2026-09-16
    # incident's own loss was small (-$16.57) and this trades away more upside
    # than it removes downside on the numbers actually measured.
    reversal_confirm_close: bool = False

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

# 8-slot prop variant (2026-09-02, Anton, "maximally prop scheme"): doubles the
# pyramiding cap from 4 to 8 (max_positions=8 -- the prior k x max_positions
# robustness sweep only went up to max=5, strategy-spec-S007.md sec 10.1).
# Paired with HALF the per-position risk (0.25% instead of 0.5%) in
# backtest/run_s007_propscheme.py, so the two configs (this vs
# WORKING_S007_NEWSSAFE @ 0.5%/R) intend the SAME per-day risk budget split
# into finer slices -- NOT more risk.
#
# TESTED 2026-09-02 (backtest/run_s007_propscheme.py, Dukascopy 2023-06-26..
# 2026-08-11, real spread 0.635/side) and ACCEPTED -- raising the cap changes
# real trading behaviour, not just sizing math: avg positions/day jumps
# 4.31->7.42 (the 4-slot cap was silently truncating real CHoCH-driven adds
# on strongly trending days), and gross expectancy at HALVED risk still more
# than doubles: net R/day (no BE) 4-slot@0.5% +0.8665 vs 8-slot@0.25% +1.9733
# (per-R; %-of-account terms are what matters for sizing and are compared via
# Gate 3 below), maxDD -53.5R -> -35.8R, worst day -9.03R -> -17.81R (in R,
# but at half the %/R the %-of-account worst day is ~unchanged: -4.52% vs
# -4.45%, no-BE). So finer granularity is closer to a free structural
# improvement than a trade-off on the raw-R axis.
#
# Gate 3 (prop survivability, block-bootstrap Monte Carlo, N=4000 paths,
# cashout +9% / daily limit -3% / total DD -10%, execution-friction modelled
# as FILL_PROB=0.75 per position -- Anton's own "3 of 4" / "6 of 8" numbers,
# both exactly 75%, emulated by simply dropping ~25% of realized positions at
# random rather than new engine fill mechanics, per Anton's explicit
# instruction): 8-slot@0.25% beats 4-slot@0.5% at EVERY breakeven level:
#   no BE:    4-slot cashout 62.5% (daily-bust 35.7%) -> 8-slot cashout 86.9% (daily-bust 13.1%)
#   BU@0.5R:  4-slot cashout 92.8% (daily-bust  3.8%) -> 8-slot cashout 100.0% (daily-bust 0.0%)
#   BU@1.0R:  4-slot cashout 73.8%                    -> 8-slot cashout 96.9%
#   BU@1.5R:  4-slot cashout 69.5%                    -> 8-slot cashout 93.1%
#   BU@2.0R:  4-slot cashout 66.8%                    -> 8-slot cashout 90.6%
# BU@0.5R is the best breakeven level for BOTH schemes on this Gate-3 axis --
# the OPPOSITE of the raw-expectancy verdict (breakeven_at_r comment above:
# REJECTED for net R at r=1.0, worse at 0.5R specifically on raw R). BU
# trades expectancy for tail protection; Gate 3 cares about the tail, not the
# average, so the two verdicts are not in conflict -- see experiments-log.md
# S007 E5 for the full reconciliation. 8-slot@0.25%+BU@0.5R's worst
# historical day (ALL positions filling, no MC dropout) is -2.70% of
# account -- structurally never breaches the -3% daily limit in this sample,
# hence its 0.0% daily-bust rate; this is a property of the specific
# historical worst day, not a guarantee about days not yet seen.
# CAVEATS: FILL_PROB=0.75 is Anton's own estimate, not independently
# measured; the daily-limit check is EOD-close only (no intraday marking, so
# real breach risk is understated, same caveat as WORKING_S007_NEWSSAFE
# above); max_positions=8 is real historical backtested behaviour but still
# only ~1 dataset/~3 years, same single-instrument/single-vendor caveats as
# the rest of S007. NOT yet promoted to any bot config -- candidate pending
# Anton's call, same status as WORKING_S007_PROP.
WORKING_S007_NEWSSAFE_MAX8 = WORKING_S007_NEWSSAFE.with_(max_positions=8)

# The full "maximally prop" scheme (ALGODEV-37): the MAX8 preset above PLUS
# breakeven at 0.5R -- the exact winning combo from the Gate-3 table in
# WORKING_S007_NEWSSAFE_MAX8's comment (8-slot@0.25% + BU@0.5R: 100.0%
# cashout / 0.0% daily-bust / worst historical day -2.70% of account,
# backtest/run_s007_propscheme.py, 2026-09-02). The harness built this combo
# ad hoc via .with_(breakeven_at_r=be); this preset names it so the live bot
# config (bot/s007_config.py::PRESET) can reference it directly. All caveats
# from the MAX8 comment apply unchanged (FILL_PROB=0.75 unmeasured -- since
# cross-checked at ~0.735 realized fill rate on live demo logs 2026-08-06..
# 31, see decisions-log.md 2026-09-02; EOD-only daily-limit check; single
# instrument/vendor). Requires live breakeven-amend support in the bot
# (bot/ctrader_s007.py + bot/s007_paper.py, ALGODEV-37) before promotion.
WORKING_S007_NEWSSAFE_MAX8_BE05 = WORKING_S007_NEWSSAFE_MAX8.with_(breakeven_at_r=0.5)

# ALGODEV-43 (2026-09-10, Anton: "make the BE a few points more favourable so
# even tiny losses don't happen -- they eat the balance too"): MAX8_BE05 with
# the breakeven stop placed 3 points PAST entry instead of exactly at entry, so
# a BE exit clears the ~1.27pt round-trip spread + commission and books a small
# WIN instead of a small loss. 3.0pt is the best of the swept offsets
# (0/1/2/3/5pt, backtest/run_s007_breakeven.py, Dukascopy 2023-06-26..
# 2026-08-11, real spread 0.635/side) -- see breakeven_offset_points' own
# verdict comment for the full table. Against MAX8_BE05 at the same Gate 2
# costs and the same 0.25%/R sizing the MAX8 family is intended for:
#   net    +1.6513 -> +1.6623 R/day  (+0.7%)
#   sum    +1122.9 -> +1130.4 R
#   maxDD    -19.2 ->   -15.9 R      (-17%, the clearest gain)
#   days+       69% ->     71%
#   worst_yr +173.9 -> +154.3 R      (-11%, the clearest cost)
#   BE-exit trades  1269 @ -87.5R total  ->  1370 @ +101.0R total
# NOT RECOMMENDED over MAX8_BE05 on the numbers alone: +0.7% net is inside
# noise (5pt is worse than 3pt on this base while better on BASELINE_S007 --
# no located optimum), and it gives up worst-year to buy maxDD. It IS the
# right preset if the goal is Anton's stated one -- never book a sub-spread
# loss on a breakeven exit -- since it delivers that at no measurable cost to
# expectancy. Gate 3 (prop cashout% / daily-bust%, the axis MAX8_BE05 was
# actually chosen on, backtest/run_s007_propscheme.py) NOT re-run for this
# preset: the offset's maxDD/win-rate improvements should if anything help
# there, but that is an expectation, not a measurement -- re-run Gate 3 before
# any promotion. NOT promoted: bot/s007_config.py::PRESET and every DB row are
# unchanged; the live cutover is Anton's call.
WORKING_S007_NEWSSAFE_MAX8_BE05_OFF3 = WORKING_S007_NEWSSAFE_MAX8_BE05.with_(
    breakeven_offset_points=3.0)

# ALGODEV-43 follow-up (2026-09-11, Anton's explicit deploy decision): asked
# for 1.0pt first, but the swept BE-exit bucket at 1.0pt is STILL net negative
# (-20.86R total / -0.016R avg on this preset -- 1pt doesn't cover the 1.27pt
# round-trip spread). 2.0pt is the smallest swept offset where BE-exits are
# actually positive (+42.13R total / +0.031R avg), so it's the minimum that
# satisfies the stated goal ("BE exits should never book a loss"), shown the
# tradeoff (net R/day -1.4% vs MAX8_BE05, maxDD -17.0R vs -19.2R, worst_yr
# +159.1 vs +173.9) and chose it anyway -- see breakeven_offset_points' and
# OFF3's comments above for the full sweep table and mechanism. THIS preset
# (not OFF3) is the one wired live -- see webapp DB account_strategies.preset
# for account_strategy id=1 (S007, demo ctrader-47939312).
WORKING_S007_NEWSSAFE_MAX8_BE05_OFF2 = WORKING_S007_NEWSSAFE_MAX8_BE05.with_(
    breakeven_offset_points=2.0)

# --- Exact reproductions of the two reference result files (regression only) ---
# NOTE (ALGODEV-43): neither REF preset sets breakeven_at_r, so the breakeven
# block in engine.py never runs for them and breakeven_offset_points is dead
# code on this path -- the regression reproduces history byte-for-byte
# regardless of the new field. Verified 2026-09-10: per-day day_R and the
# n_pos/n_tp/n_stop/n_eod counts are BIT-identical (max |diff| = 0.0, not just
# ~1e-14) before/after the offset change for both REF presets (and for
# BASELINE_S007 / WORKING_S007_LIQFLOOR / WORKING_S007_NEWSSAFE_MAX8_BE05,
# which DOES use breakeven but at the default offset 0.0).
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
