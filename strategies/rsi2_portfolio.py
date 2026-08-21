"""S011/RSI(2) — multi-asset portfolio engine.

Single-strategy backtests (see `strategies/rsi2.py`) answer "does RSI(2) have
an edge on THIS instrument". This module answers a different question,
requested directly by Anton during the S011 portfolio research (2026-08-18):
"what would ONE account look like trading RSI(2) on ALL selected assets at
once, with no signal on an asset meaning that asset is simply not traded
today?"

Position sizing (the part that matters most here -- three schemes were tried
and compared during research, see `claude/experiments-log.md` 2026-08-18):

  1. Equal-weight among currently-active signals, uncapped ("winner takes
     all" when only one signal is active) -- REJECTED: concentrates the
     whole account in a single asset (often crypto) on days with few
     signals, producing unrealistic single-day swings.
  2. Equal-weight among active signals, capped at `cap_pct` of equity,
     RE-WEIGHTED DAILY -- workable, but shrinks an already-open winning
     position's dollar size every time an unrelated new signal opens
     elsewhere (because the daily rebalance redistributes weight across
     however many positions are open that day).
  3. **This module**: each new position is sized at `cap_pct` of the
     account's CURRENT equity at the moment it is opened (or less, if less
     cash is free), then held at that dollar size -- compounding with its
     own trade's return -- until it exits. No daily rebalancing across
     positions. This is what a simple real bot would most naturally do
     ("risk cap_pct of the account on this signal, right now") and is the
     scheme this module implements.

No per-position stop-loss / no take-profit / no leverage (entries are capped
at available cash, never borrow) -- same all-in/all-out convention as
`strategies/rsi2.py` itself has no protective stop (see that module's
docstring); this engine does not add one on the single-position level.

Optional PORTFOLIO-level circuit breakers (both default `None` = off, per
`strategy-modifiers` convention: base behaviour above is unchanged unless
explicitly set). On breach, either breaker force-liquidates EVERY open
position at that day's close (paying normal exit cost) and simply waits for
each asset's own signal to fire again -- `prev_held` is deliberately left
untouched on breach (still reflecting the signal's own held/flat state) so
the next day's entry check does NOT fire while the signal is still "on"; the
account only re-enters once the underlying signal itself exits and
re-triggers. No separate "cooldown" bookkeeping is needed for this.

  - `daily_loss_limit_pct` -- single BAD DAY trigger: if the account's
    day-over-day equity (close-to-close; no intraday prices exist for this
    EOD engine) drops by this fraction or more in ONE day, liquidate.
    Requested by Anton 2026-08-18 as "SL 2% в день максимум". TESTED at
    2%/3%/4% and REJECTED at all three levels -- see
    `PORTFOLIO_DAILY_STOP_2PCT` below and `claude/experiments-log.md` S011 E6.

  - `consecutive_loss_days_limit` -- BAD STREAK trigger: if the account
    closes lower than the prior day (any amount) for this many consecutive
    days, liquidate; the streak counter resets to 0 both on any up/flat day
    and immediately after a breach. Requested by Anton 2026-08-18 as a
    follow-up to E6 specifically because the single-bad-day trigger did not
    touch the strategy's actual MaxDD (built from many small down-days in a
    row, e.g. March 2020), while this trigger targets that shape directly.
    See `PORTFOLIO_LOSS_STREAK_*` presets below and `claude/experiments-log.md`
    S011 E7 for the tested verdict.

Costs: `commission_bps + spread_bps` (round-trip, matching the convention
used everywhere else in the S011 research) is charged on the notional at
BOTH entry and exit -- i.e. paid twice per round trip, same convention as
the single-asset cost model in `backtest/run_rsi2.py`. The same cost is
charged on a forced circuit-breaker liquidation (it is a real market exit,
not a free reset).

Verified no-look-ahead (Gate 0): truncating the input data to a past cutoff
reproduces the equity curve up to that cutoff exactly (max|Δequity| = 0.0),
because entries/exits/sizing on day t only ever read `positions[t]` (already
a `.shift(1)`-decided value from the underlying signal) and `rets[t]`; both
circuit breakers on day t only read day t's own mark-to-market equity and
day t-1's already-computed equity (plus, for the streak breaker, a running
counter derived only from past days), so neither adds any forward-looking
dependency.
"""
from __future__ import annotations

from dataclasses import dataclass, replace

import pandas as pd

DEFAULT_START_CAPITAL = 10_000.0
DEFAULT_CAP_PCT = 0.20              # max fraction of equity risked on one new position
DEFAULT_COMMISSION_BPS = 0.5
DEFAULT_SPREAD_BPS = 1.0


@dataclass(frozen=True)
class PortfolioConfig:
    """Single source of truth for the portfolio engine's constants.

    start_capital                -- account size at t0.
    cap_pct                       -- max fraction of CURRENT equity a single
                                      new position may be sized at, applied
                                      at the moment of entry only (not
                                      re-applied while the position is open).
    commission_bps                -- commission, basis points of notional,
                                      per leg.
    spread_bps                     -- spread, basis points of notional, per
                                      leg.
    daily_loss_limit_pct          -- optional single-bad-day circuit breaker
                                      (fraction, e.g. 0.02 = 2%). `None`
                                      (default) = off. See module docstring.
    consecutive_loss_days_limit   -- optional bad-streak circuit breaker
                                      (integer number of consecutive down
                                      days). `None` (default) = off. See
                                      module docstring.
    """
    start_capital: float = DEFAULT_START_CAPITAL
    cap_pct: float = DEFAULT_CAP_PCT
    commission_bps: float = DEFAULT_COMMISSION_BPS
    spread_bps: float = DEFAULT_SPREAD_BPS
    daily_loss_limit_pct: float | None = None
    consecutive_loss_days_limit: int | None = None

    def __post_init__(self):
        if self.daily_loss_limit_pct is not None and not (0.0 < self.daily_loss_limit_pct < 1.0):
            raise ValueError(
                f"daily_loss_limit_pct must be in (0, 1) or None, got {self.daily_loss_limit_pct!r}")
        if self.consecutive_loss_days_limit is not None and self.consecutive_loss_days_limit < 1:
            raise ValueError(
                f"consecutive_loss_days_limit must be >= 1 or None, "
                f"got {self.consecutive_loss_days_limit!r}")

    @property
    def cost_rate(self) -> float:
        return (self.commission_bps + self.spread_bps) / 1e4


PORTFOLIO_BASELINE = PortfolioConfig()                                       # frozen champion, no circuit breaker
PORTFOLIO_DAILY_STOP_2PCT = replace(PORTFOLIO_BASELINE, daily_loss_limit_pct=0.02)
PORTFOLIO_DAILY_STOP_3PCT = replace(PORTFOLIO_BASELINE, daily_loss_limit_pct=0.03)
PORTFOLIO_DAILY_STOP_4PCT = replace(PORTFOLIO_BASELINE, daily_loss_limit_pct=0.04)
# TESTED 2026-08-18, ALL THREE REJECTED (see claude/experiments-log.md, S011 E6):
# on the same frozen WALK_FORWARD_UNIVERSE / 2021+ window --
#   2%: 10 triggers/5.63y, CAGR 8.15%->6.71%, Sharpe 0.81->0.73, MaxDD ~unchanged
#   3%: only 4 triggers/5.63y, yet CAGR 8.15%->6.11% -- WORSE than the 2% preset
#       despite firing less often: the 4 days it catches (2024-04-13, 2025-04-07,
#       2025-09-25, 2025-11-04) are exactly the biggest single-day drops, which
#       for a mean-reversion strategy tend to be followed by the strongest
#       snapback -- flattening AT THE TROUGH cuts off the best recoveries.
#   4%: 0 triggers/5.63y (max single-day loss observed was ~3.2%) -- identical
#       to PORTFOLIO_BASELINE by construction, not a real test of anything.
# In all three cases MaxDD is essentially UNCHANGED from baseline (~-10.87%) --
# the drawdown is built from many small down-days, not one big one, so a
# same-day circuit breaker structurally cannot catch it, and for a
# mean-reversion engine it actively fights the edge on the rare days it does
# fire. Kept here (not deleted) as documented, already-tested negative results
# per the strategy-modifiers convention -- PORTFOLIO_BASELINE remains the
# champion config used everywhere else (backtest/run_rsi2_portfolio.py).

PORTFOLIO_LOSS_STREAK_3D = replace(PORTFOLIO_BASELINE, consecutive_loss_days_limit=3)
PORTFOLIO_LOSS_STREAK_5D = replace(PORTFOLIO_BASELINE, consecutive_loss_days_limit=5)
PORTFOLIO_LOSS_STREAK_7D = replace(PORTFOLIO_BASELINE, consecutive_loss_days_limit=7)
# TESTED 2026-08-18, ALL THREE REJECTED (see claude/experiments-log.md, S011 E7):
# on the same frozen WALK_FORWARD_UNIVERSE / 2021+ window --
#   3d: 62 triggers/5.63y (a 13-asset portfolio drifts down 3 days running from
#       pure noise very often -- this is not a rare-event breaker, it fires
#       almost every few weeks), CAGR 8.15%->5.18%, Sharpe 0.81->0.58, and MaxDD
#       got WORSE (-10.87%->-11.92%) -- forced flatten+re-entry whipsaws added a
#       new source of loss instead of preventing one.
#   5d: 9 triggers/5.63y, still worse on every metric (CAGR 7.14%, Sharpe 0.74)
#       and MaxDD still WORSE (-11.61%).
#   7d: only 3 triggers/5.63y, closest to baseline but still strictly worse
#       (CAGR 7.58% vs 8.15%, Sharpe 0.77 vs 0.81), MaxDD unchanged (-10.87%).
# IMPORTANT CAVEAT: the WALK_FORWARD_UNIVERSE test window starts 2021-01-01 and
# so does NOT include March 2020, the actual worst historical drawdown episode
# referenced in E1/E2 (from the full-history single-asset backtests) -- this
# preset family has only been tested against the (smaller) 2021+ drawdowns, not
# against the specific long-grinding-drawdown shape it was designed to catch.
# Kept here as documented, already-tested negative results per the
# strategy-modifiers convention -- PORTFOLIO_BASELINE remains the champion
# config used everywhere else (backtest/run_rsi2_portfolio.py).

PORTFOLIO_PROP_15PCT = replace(PORTFOLIO_BASELINE, cap_pct=0.15)
# TESTED 2026-08-18, KEPT as the recommended prop-account preset (see
# claude/experiments-log.md, S011 E8): E6/E7 showed that circuit breakers
# (single-day or multi-day-streak forced liquidation) do NOT reduce this
# strategy's MaxDD and often make it WORSE, because they fight the
# mean-reversion edge instead of just reducing exposure. A cap_pct sweep
# (0.20 baseline down to 0.05) on the same frozen WALK_FORWARD_UNIVERSE showed
# Sharpe/Calmar IMPROVE as cap_pct shrinks (concentration risk falls faster
# than return), not just a linear cost -- 0.15 keeps MaxDD (-8.01%) and worst
# single day (-2.40%) comfortably under a typical FTMO-style 10%/5% prop limit
# (safety margin, not just barely under) while giving up only ~0.4pp of CAGR
# (8.15%->7.79%) and actually IMPROVING Sharpe (0.81->0.95) vs PORTFOLIO_BASELINE.
# Caveat: tested window is 2021+ only (no March-2020-style tail event) and this
# is an EOD close-to-close approximation -- live prop enforcement is real-time
# on broker equity, so this sizing choice does not replace an operational
# real-time equity monitor at the execution layer.

ALL_PORTFOLIO_PRESETS = {
    "baseline": PORTFOLIO_BASELINE,
    "daily_stop_2pct": PORTFOLIO_DAILY_STOP_2PCT,
    "daily_stop_3pct": PORTFOLIO_DAILY_STOP_3PCT,
    "daily_stop_4pct": PORTFOLIO_DAILY_STOP_4PCT,
    "loss_streak_3d": PORTFOLIO_LOSS_STREAK_3D,
    "loss_streak_5d": PORTFOLIO_LOSS_STREAK_5D,
    "loss_streak_7d": PORTFOLIO_LOSS_STREAK_7D,
    "prop_15pct": PORTFOLIO_PROP_15PCT,
}


def simulate_compounding_portfolio(
    positions: dict[str, pd.Series],
    rets: dict[str, pd.Series],
    cfg: PortfolioConfig = PortfolioConfig(),
) -> pd.Series:
    """Simulate ONE account trading every asset in `positions`/`rets` at once.

    `positions[asset]` must already be the DECIDED held-state per bar (i.e.
    the caller's `.shift(1)` of the raw strategy signal -- same convention
    as every single-asset signal function in `strategies/`). `rets[asset]`
    is that asset's simple close-to-close return series. Both are reindexed
    onto the union of all assets' dates; missing days are treated as "this
    asset did not trade today" (held=0, ret=0) for that asset only.

    Returns the account's daily equity curve (float, indexed by date).
    """
    all_dates = sorted(set().union(*(s.index for s in positions.values())))
    idx = pd.DatetimeIndex(all_dates)
    assets = list(positions.keys())

    held = pd.DataFrame({a: positions[a].reindex(idx) for a in assets}).fillna(0.0)
    ret = pd.DataFrame({a: rets[a].reindex(idx) for a in assets}).fillna(0.0)

    cash = cfg.start_capital
    position_value = {a: 0.0 for a in assets}
    prev_held = {a: 0 for a in assets}
    equity_history = []
    loss_streak = 0   # consecutive down-days counter, used only by consecutive_loss_days_limit

    for date in idx:
        held_today = {a: held.at[date, a] for a in assets}

        # 1. Exits: assets held yesterday but not today were closed at
        #    yesterday's close -- realise their current notional into cash.
        for a in assets:
            if prev_held[a] == 1 and held_today[a] == 0:
                notional = position_value[a]
                cash += notional - notional * cfg.cost_rate
                position_value[a] = 0.0

        # 2. Entries: assets newly signaled today are sized at cfg.cap_pct of
        #    equity AFTER today's exits, BEFORE today's return is applied.
        #    Multiple same-day entries all size off this same pre-entry
        #    equity snapshot; if free cash is short of the cap_pct target,
        #    the entry is sized down to whatever cash remains (no leverage).
        equity_pre_entry = cash + sum(position_value.values())
        for a in assets:
            if prev_held[a] == 0 and held_today[a] == 1:
                target = cfg.cap_pct * equity_pre_entry
                size = min(target, cash)
                position_value[a] = size - size * cfg.cost_rate
                cash -= size

        # 3. Mark to market: apply today's return to everything held today.
        for a in assets:
            if held_today[a] == 1:
                position_value[a] *= (1 + ret.at[date, a])

        equity_today = cash + sum(position_value.values())
        equity_prev = equity_history[-1] if equity_history else cfg.start_capital
        breach = False

        # 4. Optional single-bad-day circuit breaker (default off -- see
        #    PortfolioConfig.daily_loss_limit_pct docstring). Compares
        #    TODAY's post-mark-to-market equity against YESTERDAY's already
        #    -settled equity (or start_capital on day 0) -- both values are
        #    fully known by this point in day t's processing, so this adds
        #    no look-ahead.
        if cfg.daily_loss_limit_pct is not None:
            if equity_prev > 0 and (equity_today / equity_prev - 1.0) <= -cfg.daily_loss_limit_pct:
                breach = True

        # 5. Optional bad-streak circuit breaker (default off -- see
        #    PortfolioConfig.consecutive_loss_days_limit docstring). Any
        #    down day (equity_today < equity_prev, however small) extends
        #    the streak; any up/flat day resets it to 0. A breach also
        #    resets it to 0 (the forced liquidation itself is not counted
        #    as a fresh "down day" for the next streak).
        if cfg.consecutive_loss_days_limit is not None:
            if equity_today < equity_prev:
                loss_streak += 1
            else:
                loss_streak = 0
            if loss_streak >= cfg.consecutive_loss_days_limit:
                breach = True
                loss_streak = 0

        # On breach (either breaker): force-close every open position at
        # today's close, paying the normal exit cost; `prev_held` is
        # deliberately left as `held_today` below (see module docstring) so
        # the account only re-enters once the underlying signal re-triggers.
        if breach:
            for a in assets:
                if position_value[a] > 0:
                    notional = position_value[a]
                    cash += notional - notional * cfg.cost_rate
                    position_value[a] = 0.0
            equity_today = cash

        equity_history.append(equity_today)
        prev_held = held_today

    return pd.Series(equity_history, index=idx, name="equity")
