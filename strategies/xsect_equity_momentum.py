"""S027 -- US Sector ETF Cross-Sectional Momentum.

Idea, AS ACTUALLY SPECIFIED on Confluence "Strategies" -> P12 / S025+S026+S027 (page
id 24838145, S027 heading "US Sector ETF Cross-Sectional Momentum (месячный
ребаланс)"): classic Jegadeesh & Titman (1993) cross-sectional momentum on the 11
SPDR US sector ETFs -- rank by trailing 6-12 month return (skipping the most recent
month, the standard reversal guard), buy the top-N sectors equal-weight, LONG-ONLY,
rebalanced once a month. This is genuinely different from S009/S012's dollar-neutral
long+short book: the spec explicitly calls out "несёт направленную бету на
американский рынок акций, а не market-neutral позицию" (carries directional US
equity beta, not a market-neutral position) -- that is stated as a fact about the
strategy, not hidden.

Implementation history (kept here so a later session doesn't repeat the mistake):
this module's first draft (same session, same day) built a DAILY-rebalanced,
DOLLAR-NEUTRAL long/short book by mechanically reusing xsect_base.rank_weights() --
i.e. it copied S012's mechanics onto a new universe without re-reading what S027
itself specifies. That version is kept below as
``run_backtest_dollar_neutral_daily_variant`` (still real, still tested) because it
is an honest, separately-labeled EXPLORATORY variant -- same engine, no short-vol
directional beta, different question -- but ``run_backtest`` (the name a caller
reaches for) now means the ACTUALLY SPECIFIED monthly/long-only version, matching the
Confluence page rather than the more mechanically obvious reuse.

Monthly-rebalance convention: on the last trading day of each calendar month, rank
the momentum signal (built from data through, and including, that day -- fully
realized, no shift needed for the SAME reason S005's monthly FX-carry rebalance
needs none) and pick the top-N. That decision is applied starting the NEXT trading
day (one extra shift(1) makes this explicit and look-ahead-proof rather than relying
on "well, it's month-end, so it should be fine") and held constant until the
following month's decision takes effect the same way. Gate 0 below checks this
holds under truncation, the same regression discipline as every other strategy here.

Formation window: default 126/21 trading days (~6 months lookback, ~1 month skip),
the low end of the spec's stated 6-12 month range (McLean & Pontiff 2016 note this
factor's post-publication decay -- also stated on the Confluence page, repeated here
because it is the single most important caveat for this strategy). The other end
(~252/21, the canonical 12-1 month JT variant) is one ``.with_()`` call away
(``cfg.with_(lookback_days=252)``) -- not run here, since picking between them without
a real walk-forward would just be parameter shopping.

Deliberately reuses ``portfolio_returns`` from xsect_base.py for BOTH functions in
this module (pure, periodicity-agnostic per-day return/turnover/cost math -- correct
whether weights change every day or once a month) but NOT ``rank_weights`` for the
long-only book (it always builds two legs, long_w and short_w, dividing by
``bottom_n`` unconditionally -- structurally cannot produce a long-only book without
a fragile zero-division workaround) and NOT ``xsect_base.metrics()`` (hardcodes 365
calendar days/year for always-on crypto; a ~252-day/year equity book needs
``utils.report.make_report(..., periods_per_year=TRADING_DAYS_PER_YEAR)`` instead,
the convention this project already uses for fx_carry/overnight_drift).
xsect_base.py itself is left untouched throughout: it backs the currently-live S009
book, and neither S027 variant has any need to touch code a live strategy depends on.

Data note: XLRE (Real Estate) only exists since 2015-10-08 and XLC (Communication
Services) only since 2018-06-19 -- real SPDR launch dates, not a data gap. Before
those dates the universe is naturally 10 and then 9 names; ``min_universe`` is what
keeps either book from trading on too few valid names on any given rebalance.
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass

import numpy as np
import pandas as pd

from strategies.xsect_base import portfolio_returns, rank_weights
from utils.data import load_csv

TRADING_DAYS_PER_YEAR = 252

DEFAULT_UNIVERSE = (
    "XLE", "XLF", "XLK", "XLV", "XLI", "XLY", "XLP", "XLU", "XLB", "XLRE", "XLC",
)


@dataclass(frozen=True)
class XSectEquityMomentumConfig:
    universe: tuple = DEFAULT_UNIVERSE
    top_n: int = 3                    # LONG the N best trailing performers (both variants)
    bottom_n: int = 3                 # SHORT the N worst (dollar-neutral variant ONLY)
    lookback_days: int = 126          # ~6 trading months, low end of the spec's 6-12mo range
    skip_days: int = 21               # ~1 trading month reversal guard
    min_universe: int = 6             # need at least this many valid sectors to trade
    gross_leverage: float = 1.0       # long-only: total invested fraction; dollar-neutral: scales +-0.5 books
    cost_bps_per_side: float = 0.0    # baseline gross; E3 cost runs set this > 0

    def with_(self, **changes) -> "XSectEquityMomentumConfig":
        return dataclasses.replace(self, **changes)


def load_sector_panel(universe: tuple = DEFAULT_UNIVERSE) -> pd.DataFrame:
    """Load daily closes for every ticker in ``universe`` from
    ``data/raw/<TICKER>/<TICKER>_1d.csv`` (utils.data.load_yf's cache layout) into one
    wide DataFrame, columns = tickers. A ticker with no history yet on a given date
    (XLRE pre-2015, XLC pre-2018) is NaN there, not zero -- both weight-construction
    functions below drop NaN before ranking, and ``min_universe`` keeps thin days flat.
    """
    closes = {}
    for ticker in universe:
        df = load_csv(f"{ticker}/{ticker}_1d.csv")
        closes[ticker] = df["close"]
    return pd.DataFrame(closes).sort_index()


# ---------------------------------------------------------------------------
# Canonical S027: monthly rebalance, long-only top-N (matches the Confluence spec)
# ---------------------------------------------------------------------------

def _month_end_trading_days(index: pd.DatetimeIndex) -> pd.DatetimeIndex:
    """The last trading day actually present in ``index`` for each calendar month."""
    periods = pd.Series(index, index=index).groupby(index.to_period("M")).max()
    return pd.DatetimeIndex(periods.to_numpy())


def monthly_momentum_signal(close: pd.DataFrame, cfg: XSectEquityMomentumConfig) -> pd.DataFrame:
    """Trailing return over [d-skip-lookback, d-skip], AS OF day d itself (no extra
    shift -- see module docstring for why none is needed here, unlike the daily
    variant below)."""
    return close.shift(cfg.skip_days) / close.shift(cfg.skip_days + cfg.lookback_days) - 1.0


def _long_only_top_n_weights_at(row: pd.Series, cfg: XSectEquityMomentumConfig) -> pd.Series:
    valid_row = row.dropna()
    weights = pd.Series(0.0, index=row.index)
    if len(valid_row) < cfg.min_universe:
        return weights
    top = valid_row.sort_values(ascending=False).index[: cfg.top_n]
    weights.loc[top] = cfg.gross_leverage / cfg.top_n
    return weights


def run_backtest(close: pd.DataFrame, cfg: XSectEquityMomentumConfig) -> tuple[pd.DataFrame, pd.DataFrame]:
    """THE spec'd S027: decide the book on the last trading day of each month from
    that day's fully-realized momentum signal, apply it starting the NEXT trading
    day, hold constant through the following month's decision. Long-only, equal
    weight across the top ``cfg.top_n`` sectors; ``bottom_n`` is unused here.

    Returns (out, w) exactly like the dollar-neutral variant below: out has
    gross_ret/net_ret/turnover/n_pos, w is the (mostly flat, occasionally jumping)
    daily weight matrix.
    """
    signal = monthly_momentum_signal(close, cfg)
    rebal_dates = _month_end_trading_days(close.index)
    rebal_dates = rebal_dates[rebal_dates.isin(signal.dropna(how="all").index)]

    decisions = pd.DataFrame(0.0, index=rebal_dates, columns=close.columns)
    for d in rebal_dates:
        decisions.loc[d] = _long_only_top_n_weights_at(signal.loc[d], cfg)

    w = decisions.reindex(close.index)
    w = w.shift(1)          # decision made AT d takes effect starting the NEXT trading day
    w = w.ffill().fillna(0.0)

    price_ret = close.pct_change()
    funding_day = pd.DataFrame(0.0, index=close.index, columns=close.columns)
    valid = price_ret.notna()
    return portfolio_returns(
        w, price_ret, funding_day, valid,
        taker_fee_per_side=cfg.cost_bps_per_side / 1e4,
    )


def gate0_no_look_ahead(close: pd.DataFrame, cfg: XSectEquityMomentumConfig,
                         cut_days: int = 200) -> float:
    """No-look-ahead regression check on the canonical (monthly, long-only)
    backtest: net return already realized on day t must not change when everything
    strictly after day t is deleted. Returns max|delta| over the overlapping
    prefix -- must be exactly 0.0."""
    full, _ = run_backtest(close, cfg)
    truncated_input = close.iloc[: len(close) - cut_days]
    truncated, _ = run_backtest(truncated_input, cfg)
    common = full.index.intersection(truncated.index)
    return float((full.loc[common, "net_ret"] - truncated.loc[common, "net_ret"]).abs().max())


# ---------------------------------------------------------------------------
# Exploratory variant (first draft, kept for the record -- see module docstring):
# daily rebalance, dollar-neutral long/short, mechanical S012-style engine reuse.
# NOT what the Confluence page specifies for S027 -- do not present as "the" S027.
# ---------------------------------------------------------------------------

def daily_momentum_signal(close: pd.DataFrame, cfg: XSectEquityMomentumConfig) -> pd.DataFrame:
    """Same shape as xsect_momentum.momentum_signal (S012): the day-d signal is
    shifted once more than monthly_momentum_signal, because here it is applied to
    price_ret on that SAME day d (see run_backtest_dollar_neutral_daily_variant)."""
    ret_window = close.shift(cfg.skip_days) / close.shift(cfg.skip_days + cfg.lookback_days) - 1.0
    return ret_window.shift(1)


def run_backtest_dollar_neutral_daily_variant(
    close: pd.DataFrame, cfg: XSectEquityMomentumConfig,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Exploratory only (see module docstring): daily-rebalanced, dollar-neutral
    long top_n / short bottom_n book, built by reusing xsect_base.rank_weights()
    verbatim -- structurally identical to S012 on a different universe."""
    price_ret = close.pct_change()
    signal = daily_momentum_signal(close, cfg)
    funding_day = pd.DataFrame(0.0, index=close.index, columns=close.columns)
    valid = price_ret.notna() & signal.notna()

    w = rank_weights(
        signal, valid,
        top_n=cfg.top_n, bottom_n=cfg.bottom_n,
        min_universe=cfg.min_universe, gross_leverage=cfg.gross_leverage,
        long_top=True,
    )
    return portfolio_returns(
        w, price_ret, funding_day, valid,
        taker_fee_per_side=cfg.cost_bps_per_side / 1e4,
    )


def gate0_no_look_ahead_daily_variant(close: pd.DataFrame, cfg: XSectEquityMomentumConfig,
                                       cut_days: int = 200) -> float:
    """Same regression check as gate0_no_look_ahead, for the exploratory variant."""
    full, _ = run_backtest_dollar_neutral_daily_variant(close, cfg)
    truncated_input = close.iloc[: len(close) - cut_days]
    truncated, _ = run_backtest_dollar_neutral_daily_variant(truncated_input, cfg)
    common = full.index.intersection(truncated.index)
    return float((full.loc[common, "net_ret"] - truncated.loc[common, "net_ret"]).abs().max())
