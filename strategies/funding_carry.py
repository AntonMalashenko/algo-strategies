"""S009 — crypto funding-carry: cross-sectional, market-neutral engine.

Idea: perpetual funding is a periodic payment between longs and shorts. We rank a
basket of perps by trailing funding, SHORT the highest-funding names (collect
funding + fade crowded longs) and LONG the lowest/most-negative (collect funding
as a long), equal-weight and dollar-neutral. Return of a leg = the perp's price
move PLUS accrued funding — the funding term is the carry (cf. the rate term in
S005 FX carry). No spot needed; the cross-section hedges market beta.

Design (parametric; baseline = FundingCarryConfig()):
  - Daily rebalance at 00:00 UTC.
  - Signal at day d = trailing mean daily funding through day d-1 (STRICTLY past —
    no look-ahead). Positions held during day d; return uses day-d price move and
    funding accrued during day d.
  - Weights: long book +0.5, short book -0.5 (gross 1.0, dollar-neutral), scaled
    by gross_leverage.
  - Costs: turnover x taker_fee_per_side (0 in the E2 baseline).

Accounting convention (derivation): holding weight w in a coin with day price
return p and accrued funding f (longs pay f when f>0) earns  w*p - w*f = w*(p - f).
So a short (w<0) in a high-funding coin (f>0) earns -w*f > 0. Hence
    port_ret_d = Σ_sym w[sym] * (price_ret[sym,d] - funding_day[sym,d]).

The cross-sectional plumbing (panel loading, rank weights, portfolio
accounting, metrics) lives in strategies/xsect_base.py, shared with the
pro-trend momentum sibling S012 (ALGODEV-13). This module keeps its public
API (load_panels, run_backtest, compute_weights, metrics, MS_PER_DAY,
DEFAULT_UNIVERSE) intact — bot/s009_paper.py and the backtest scripts import
from here unchanged.
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
class FundingCarryConfig:
    universe: tuple = DEFAULT_UNIVERSE
    top_n: int = 3                    # SHORT the N highest-funding perps
    bottom_n: int = 3                 # LONG the N lowest-funding perps
    signal_lookback_days: int = 1     # trailing window for the funding signal
    min_universe: int = 6             # need at least this many valid coins to trade
    gross_leverage: float = 1.0       # scales the +0.5/-0.5 books
    taker_fee_per_side: float = 0.0   # E2 baseline = 0; E3 sets ~0.00055

    # Optional vol-targeting modifier (default OFF → reproduces baseline byte-for-byte).
    # When >0, each day's weights are scaled so the strategy's trailing realised vol
    # matches the target, capped at vol_scale_cap. Uses only past returns (no look-ahead).
    vol_target_annual: float = 0.0    # 0 = off; e.g. 0.15 targets ~15% annualised vol
    vol_lookback_days: int = 30
    vol_scale_cap: float = 3.0        # max leverage multiple

    reserved_oos_start: str = "2025-07-20"

    def with_(self, **changes) -> "FundingCarryConfig":
        return dataclasses.replace(self, **changes)


# --------------------------------------------------------------------------
# Engine (delegates to the shared cross-sectional base)
# --------------------------------------------------------------------------

def compute_weights(signal: pd.DataFrame, valid: pd.DataFrame, cfg: FundingCarryConfig) -> pd.DataFrame:
    """Per day: short top_n by signal, long bottom_n, dollar-neutral weights."""
    return rank_weights(
        signal, valid,
        top_n=cfg.top_n, bottom_n=cfg.bottom_n,
        min_universe=cfg.min_universe, gross_leverage=cfg.gross_leverage,
        long_top=False,  # carry: long LOWEST funding, short HIGHEST
    )


def run_backtest(close: pd.DataFrame, funding_day: pd.DataFrame, cfg: FundingCarryConfig):
    """Return a DataFrame indexed by day with columns: gross_ret, net_ret, turnover, n_pos."""
    price_ret = close.pct_change()                         # p_d = C[d]/C[d-1]-1, held day d
    # Signal: trailing mean daily funding through d-1 (STRICTLY past → shift(1)).
    signal = funding_day.rolling(cfg.signal_lookback_days, min_periods=1).mean().shift(1)
    valid = price_ret.notna() & funding_day.notna() & signal.notna()

    w = compute_weights(signal, valid, cfg)
    return portfolio_returns(
        w, price_ret, funding_day, valid,
        taker_fee_per_side=cfg.taker_fee_per_side,
        vol_target_annual=cfg.vol_target_annual,
        vol_lookback_days=cfg.vol_lookback_days,
        vol_scale_cap=cfg.vol_scale_cap,
    )
