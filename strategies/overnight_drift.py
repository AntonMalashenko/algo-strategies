"""S025 -- US Index Overnight Drift.

Idea (see Confluence "Strategies" -> P12 / S025+S026+S027, page id 24838145): hold the
index only overnight -- from the prior cash close to today's cash open -- with zero
intraday exposure. This is the mechanical opposite of every intraday breakout/MR
strategy already in the project (S004/S007/S019/S021/S023): it isolates whichever part
of an index's long-run return sits in the close-to-open leg versus the open-to-close
leg, per the well-known (and contested -- see module-level caveat below) overnight vs.
intraday return-decomposition literature (Bogousslavsky 2016 and the broader
autocorrelation-of-infrequent-rebalancing line of work).

Look-ahead discipline (same convention as S004/S007/fx_carry): the overnight return
realized on day t uses ONLY `open[t]` (observed at today's cash open) and `close[t-1]`
(already closed and known before today's session started). Nothing from day t's
intraday session, and nothing from day t+1, enters the day-t overnight figure. This is
a static "always hold overnight" rule -- there is no signal to leak, but the return
attribution itself must not smear intraday information into the overnight bucket (the
opposite mistake would be using a same-day close instead of the *prior* day's close).

Honest caveat, stated up front rather than after a rosy backtest: several 2020s
re-examinations of the overnight-return anomaly find it weakened or reversed on parts
of the US equity market after the pattern became widely known (a "factor decay after
publication" risk, the same class of caveat already on record for S020/S027). Treat
any positive result here as a hypothesis to survive walk-forward, not a fact.

Data note: this module works on plain daily OHLC (open/close only, no intraday data
needed) -- for a real cash-settled index (S&P 500 composite, Nasdaq Composite) the
daily "open" IS the cash-session open and the daily "close" IS the cash-session close,
so no session-boundary/DST bookkeeping is required, unlike a 24h-traded CFD. The
project's live instrument for the S023/S021 niche is the NAS100/US500 *CFD*, which
trades close to continuously -- the CFD's own cash-session boundary (and its DST
handling) is a separate, harder problem, explicitly punted here. The daily NASDAQ
Composite / S&P 500 series already fetched under data/raw/ (used for S003/S011) is a
composite-index/CFD proxy, not the exact live instrument -- flag this every time a
number from this module is quoted, per the project's under-claim house style.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

# Round-trip cost is charged twice a day (enter the overnight position at the close,
# exit it at the open) since the strategy is flat intraday by construction -- this is
# the single most important cost sensitivity of the whole idea, named here rather than
# left as a bare literal (code-architecture: no magic values).
TRADING_DAYS_PER_YEAR = 252


def decompose_overnight_intraday(daily: pd.DataFrame) -> pd.DataFrame:
    """Split each day's cash-to-cash return into its overnight and intraday legs.

    Parameters
    ----------
    daily : DataFrame with an ``open``/``close`` column and a DatetimeIndex, one row
        per trading day (as produced by ``utils.data.load_csv``).

    Returns
    -------
    DataFrame indexed like ``daily`` (first row dropped -- it has no prior close),
    columns:
      - ``overnight_ret``: open[t] / close[t-1] - 1 (return earned holding from the
        prior close to today's open; realized as soon as today's open prints).
      - ``intraday_ret``: close[t] / open[t] - 1 (return earned holding from today's
        open to today's close; not used by the strategy, kept for the decomposition).
      - ``full_day_ret``: close[t] / close[t-1] - 1, i.e. overnight + intraday
        compounded -- the ordinary buy-and-hold daily return, for comparison.
    """
    close = daily["close"]
    open_ = daily["open"]
    prior_close = close.shift(1)

    out = pd.DataFrame(index=daily.index)
    out["overnight_ret"] = open_ / prior_close - 1.0
    out["intraday_ret"] = close / open_ - 1.0
    out["full_day_ret"] = close / prior_close - 1.0
    return out.iloc[1:]


def overnight_equity_curve(daily: pd.DataFrame, cost_bps_per_side: float = 0.0,
                            start_equity: float = 1.0) -> pd.Series:
    """Compounding equity curve of "always long overnight, flat intraday".

    ``cost_bps_per_side`` models the round-trip friction of the two transactions this
    strategy makes every single day (buy at the close, sell at the next open) -- set
    it to 0.0 to reproduce the frictionless decomposition exactly (same "flag off =
    old behaviour" convention as fx_carry.py's ``spread_bps``). Each of the two legs
    (entry at close, exit at open) pays the cost once, so the daily drag is
    ``2 * cost_bps_per_side / 1e4``, subtracted from the gross overnight return.
    """
    legs = decompose_overnight_intraday(daily)
    gross = legs["overnight_ret"]
    daily_cost = 2.0 * cost_bps_per_side / 1e4
    net = gross - daily_cost
    equity = start_equity * (1.0 + net).cumprod()
    equity.name = "equity"
    return equity


def buy_and_hold_equity_curve(daily: pd.DataFrame, start_equity: float = 1.0) -> pd.Series:
    """Ordinary close-to-close buy-and-hold curve, for the "is overnight actually
    where the return lives" comparison the strategy's whole premise rests on."""
    legs = decompose_overnight_intraday(daily)
    equity = start_equity * (1.0 + legs["full_day_ret"]).cumprod()
    equity.name = "equity"
    return equity


def intraday_only_equity_curve(daily: pd.DataFrame, start_equity: float = 1.0) -> pd.Series:
    """The complementary leg: flat overnight, long only open-to-close each day."""
    legs = decompose_overnight_intraday(daily)
    equity = start_equity * (1.0 + legs["intraday_ret"]).cumprod()
    equity.name = "equity"
    return equity


def annual_breakdown(daily: pd.DataFrame) -> pd.DataFrame:
    """Per-calendar-year mean overnight vs. intraday return and hit rate -- the walk-
    forward-by-year view (Gate 1 style) rather than one blended full-sample number,
    which is what actually tells you whether the effect is a stable, recurring
    property of every year or a full-sample average dominated by a handful of them.
    """
    legs = decompose_overnight_intraday(daily)
    grouped = legs.groupby(legs.index.year)
    out = grouped.agg(
        overnight_mean=("overnight_ret", "mean"),
        overnight_hit_rate=("overnight_ret", lambda s: float((s > 0).mean())),
        intraday_mean=("intraday_ret", "mean"),
        intraday_hit_rate=("intraday_ret", lambda s: float((s > 0).mean())),
        n_days=("overnight_ret", "size"),
    )
    out.index.name = "year"
    return out


def gate0_no_look_ahead(daily: pd.DataFrame, cut_days: int = 200) -> float:
    """No-look-ahead regression check (same discipline as every other strategy in the
    project): the overnight return already realized on day t must not change when
    everything strictly after day t is deleted from the input. Returns max|delta|
    over the overlapping prefix -- must be exactly 0.0.
    """
    full = decompose_overnight_intraday(daily)["overnight_ret"]
    truncated_input = daily.iloc[: len(daily) - cut_days]
    truncated = decompose_overnight_intraday(truncated_input)["overnight_ret"]
    common = full.index.intersection(truncated.index)
    return float((full.loc[common] - truncated.loc[common]).abs().max())


def diagnose_open_data_quality(daily: pd.DataFrame, window: int = TRADING_DAYS_PER_YEAR,
                                zero_threshold: float = 0.2) -> dict:
    """Flag the single biggest practical risk of this module: on several vendor daily
    series (confirmed here for the project's NASDAQ/SP500 composite-index CSVs), the
    "open" column for older history is not an independently observed cash-session
    open at all -- it is silently backfilled as identical to the prior day's close,
    which makes ``overnight_ret`` exactly 0.0 on those days by construction rather
    than a real (near-)zero economic outcome. Any Sharpe/CAGR computed over a window
    that includes this backfilled region overstates data quality and, if the
    backfilled region dominates the sample (it does here, pre-2000s), can also
    understate or distort the *effect size* -- a blended average over "definitely 0"
    days and "real" days is not the same statistic as either one alone.

    Returns
    -------
    dict with:
      - ``frac_zero_overnight``: fraction of all rows where open[t] == close[t-1]
        exactly (to float tolerance) -- the full-sample contamination rate.
      - ``first_reliable_date``: the day after the LAST date at which the trailing
        ``window``-day rolling fraction of exact-zero days is still >= threshold --
        i.e. the first date from which contamination never re-crosses back above
        the threshold looking forward, not just the first date it happens to dip
        below it (a transient clean patch surrounded by contamination on both sides
        must not be reported as reliable). ``None`` if the series is never clean
        enough. This is a detector, not a guarantee -- inspect the surrounding
        period before trusting data after this date, and treat everything before
        it as unusable for this strategy's economics (look-ahead-clean, but not an
        honest overnight return).
    """
    legs = decompose_overnight_intraday(daily)
    is_zero = legs["overnight_ret"].abs() < 1e-12
    frac_zero_overnight = float(is_zero.mean())

    # Use the LAST breach of the threshold, not the first dip below it: a rolling
    # window can dip under the threshold transiently (noise, a short clean patch)
    # and climb back above it later, which "first date below threshold" would
    # wrongly report as reliable. The day after the last breach is the first day
    # from which the trailing window never re-crosses back above the threshold.
    rolling_frac_zero = is_zero.rolling(window).mean()
    breaches = rolling_frac_zero[rolling_frac_zero >= zero_threshold]
    if len(breaches) == 0:
        first_reliable_date = rolling_frac_zero.dropna().index.min() if rolling_frac_zero.notna().any() else None
    else:
        last_breach = breaches.index.max()
        after = rolling_frac_zero.loc[rolling_frac_zero.index > last_breach]
        first_reliable_date = after.index.min() if len(after) else None

    return {
        "frac_zero_overnight": frac_zero_overnight,
        "first_reliable_date": first_reliable_date,
    }
