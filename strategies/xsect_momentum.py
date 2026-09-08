"""S012 — cross-sectional crypto momentum: pro-trend sibling of S009 (ALGODEV-13).

Idea: rank a basket of USDT perps by trailing price return, LONG the winners
and SHORT the losers, equal-weight and dollar-neutral. Where S009
(funding-carry) fades crowding, S012 rides it — the research hypothesis is
that the two books have low correlation, which is the main value driver;
without it a good standalone Sharpe is not enough (ticket ALGODEV-13).

Design (parametric; baseline = XSectMomentumConfig()):
  - Daily rebalance at 00:00 UTC, same panels as S009
    (data/raw/crypto_funding/<SYM>/{d1,funding}.csv).
  - Signal at day d = price return over [d-1-lookback, d-1-skip], STRICTLY
    past (built from closes through d-1, then shift(1) semantics as below).
    `skip_days` optionally excludes the most recent days, the classic guard
    against short-term reversal contaminating the momentum signal.
  - Weights: long book +0.5 (highest signal), short book -0.5 (lowest),
    gross 1.0, dollar-neutral, scaled by gross_leverage.
  - Accounting: identical to S009 — leg return = w*(price_ret - funding_day),
    because a momentum perp position pays/receives funding like any other.
    Note this systematically works AGAINST momentum: winners tend to have
    positive funding, so the long book pays carry. Costs must include it.
  - Costs: turnover x taker_fee_per_side.

All cross-sectional plumbing is shared with S009 via strategies/xsect_base.py.
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass

import pandas as pd

from strategies.xsect_base import (  # noqa: F401  (re-exported public API)
    DEFAULT_UNIVERSE,
    MS_PER_DAY,
    load_panels,
    metrics,
    portfolio_returns,
    rank_weights,
)


@dataclass(frozen=True)
class XSectMomentumConfig:
    universe: tuple = DEFAULT_UNIVERSE
    top_n: int = 3                    # LONG the N best trailing performers
    bottom_n: int = 3                 # SHORT the N worst trailing performers
    lookback_days: int = 30           # momentum formation window
    skip_days: int = 0                # exclude the most recent N days (reversal guard)
    min_universe: int = 6             # need at least this many valid coins to trade
    gross_leverage: float = 1.0       # scales the +0.5/-0.5 books
    taker_fee_per_side: float = 0.0   # baseline gross; cost runs set ~0.00055

    # Optional vol-targeting modifier (default OFF), same semantics as S009.
    vol_target_annual: float = 0.0    # 0 = off; e.g. 0.15 targets ~15% annualised vol
    vol_lookback_days: int = 30
    vol_scale_cap: float = 3.0        # max leverage multiple

    # Same reserved OOS boundary as S009 so IS/OOS splits line up when
    # comparing / combining the two books.
    reserved_oos_start: str = "2025-07-20"

    def with_(self, **changes) -> "XSectMomentumConfig":
        return dataclasses.replace(self, **changes)


def momentum_signal(close: pd.DataFrame, cfg: XSectMomentumConfig) -> pd.DataFrame:
    """Trailing price return over [d-lookback-skip, d-skip], all through day
    d-1 relative to the day-d holding period (the final shift(1) makes the
    window strictly past)."""
    ret_window = close.shift(cfg.skip_days) / close.shift(cfg.skip_days + cfg.lookback_days) - 1.0
    return ret_window.shift(1)


def run_backtest(close: pd.DataFrame, funding_day: pd.DataFrame, cfg: XSectMomentumConfig):
    """Return (out, w): out indexed by day with gross_ret, net_ret, turnover, n_pos."""
    price_ret = close.pct_change()
    signal = momentum_signal(close, cfg)
    valid = price_ret.notna() & funding_day.notna() & signal.notna()

    w = rank_weights(
        signal, valid,
        top_n=cfg.top_n, bottom_n=cfg.bottom_n,
        min_universe=cfg.min_universe, gross_leverage=cfg.gross_leverage,
        long_top=True,  # momentum: long WINNERS, short LOSERS
    )
    return portfolio_returns(
        w, price_ret, funding_day, valid,
        taker_fee_per_side=cfg.taker_fee_per_side,
        vol_target_annual=cfg.vol_target_annual,
        vol_lookback_days=cfg.vol_lookback_days,
        vol_scale_cap=cfg.vol_scale_cap,
    )
