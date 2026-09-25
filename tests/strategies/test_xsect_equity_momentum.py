"""Gate 0 for S027 (US Sector ETF Cross-Sectional Momentum): no-look-ahead + sanity.

Covers BOTH functions the module exposes: the canonical monthly/long-only
``run_backtest`` (what the Confluence page actually specifies) and the exploratory
``run_backtest_dollar_neutral_daily_variant`` (first draft, kept for the record --
see strategies/xsect_equity_momentum.py's module docstring for why it exists and
why it is not "the" S027).
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from strategies.xsect_equity_momentum import (
    DEFAULT_UNIVERSE,
    XSectEquityMomentumConfig,
    _month_end_trading_days,
    daily_momentum_signal,
    gate0_no_look_ahead,
    gate0_no_look_ahead_daily_variant,
    monthly_momentum_signal,
    run_backtest,
    run_backtest_dollar_neutral_daily_variant,
)

UNIVERSE = ("XAA", "XBB", "XCC", "XDD", "XEE", "XFF", "XGG")


def _panel(n_days: int = 800, seed: int = 27, universe: tuple = UNIVERSE) -> pd.DataFrame:
    """Synthetic multi-sector daily-close panel, same shape as
    tests/strategies/test_xsect_combo.py's ``_panels()`` helper, on a business-day
    DatetimeIndex since this module's real data (load_sector_panel) is date-indexed.
    800 business days (~3.2 years) so several month-end rebalances actually happen."""
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2015-01-01", periods=n_days, freq="B")
    rets = rng.normal(0.0002, 0.012, size=(n_days, len(universe)))
    close = pd.DataFrame(100.0 * np.exp(np.cumsum(rets, axis=0)), index=idx, columns=universe)
    return close


def _cfg(**changes) -> XSectEquityMomentumConfig:
    base = XSectEquityMomentumConfig(universe=UNIVERSE, top_n=2, bottom_n=2,
                                      lookback_days=40, skip_days=5, min_universe=4)
    return base.with_(**changes) if changes else base


# ---------------------------------------------------------------------------
# Canonical: monthly rebalance, long-only
# ---------------------------------------------------------------------------

def test_monthly_no_lookahead():
    close = _panel()
    cfg = _cfg()
    max_abs_delta = gate0_no_look_ahead(close, cfg, cut_days=100)
    assert max_abs_delta == 0.0


def test_monthly_no_lookahead_multiple_cuts():
    close = _panel()
    cfg = _cfg()
    full, _ = run_backtest(close, cfg)
    for cut in (300, 500, 600, len(close) - 30):
        truncated, _ = run_backtest(close.iloc[:cut], cfg)
        common = full.index.intersection(truncated.index)
        max_abs_delta = (full.loc[common, "net_ret"] - truncated.loc[common, "net_ret"]).abs().max()
        assert max_abs_delta == 0.0, f"look-ahead: cut={cut}"


def test_monthly_weights_are_long_only_and_bounded():
    close = _panel()
    cfg = _cfg(gross_leverage=1.0)
    _, w = run_backtest(close, cfg)
    assert (w >= -1e-12).all().all()  # never short
    gross = w.abs().sum(axis=1)
    assert (gross <= cfg.gross_leverage + 1e-9).all()


def test_monthly_rebalance_dates_are_month_ends_only():
    """Weights must change ONLY on the trading day after a detected month-end --
    every other day should hold flat (turnover == 0), which is the whole point of
    a monthly-rebalance book vs. S012's daily one."""
    close = _panel()
    cfg = _cfg()
    _, w = run_backtest(close, cfg)
    changed = w.diff().abs().sum(axis=1) > 1e-12
    month_ends = set(_month_end_trading_days(close.index))
    effective_dates = {d for d in close.index if (d - pd.tseries.offsets.BDay(1)) in month_ends}
    # every day the book actually changed must be a "day after a month end"
    assert set(changed[changed].index) <= effective_dates


def test_monthly_min_universe_floor_flattens_thin_days():
    close = _panel()
    cfg = _cfg(min_universe=len(UNIVERSE) + 1)
    _, w = run_backtest(close, cfg)
    assert (w == 0.0).all().all()


def test_monthly_cost_strictly_reduces_net_vs_gross_when_turnover_is_positive():
    close = _panel()
    cfg = _cfg(cost_bps_per_side=5.0)
    out, _ = run_backtest(close, cfg)
    active = out["turnover"] > 0
    assert active.any()
    assert (out.loc[active, "net_ret"] < out.loc[active, "gross_ret"]).all()


# ---------------------------------------------------------------------------
# Exploratory: daily rebalance, dollar-neutral (first-draft variant)
# ---------------------------------------------------------------------------

def test_daily_variant_no_lookahead():
    close = _panel()
    cfg = _cfg()
    max_abs_delta = gate0_no_look_ahead_daily_variant(close, cfg, cut_days=100)
    assert max_abs_delta == 0.0


def test_daily_variant_zero_funding_reduces_to_plain_price_return():
    close = _panel()
    cfg = _cfg()
    out, w = run_backtest_dollar_neutral_daily_variant(close, cfg)
    price_ret = close.pct_change()
    valid = price_ret.notna() & daily_momentum_signal(close, cfg).notna()
    manual_gross = (w * price_ret).where(valid, 0.0).sum(axis=1)
    max_abs_delta = (out["gross_ret"] - manual_gross).abs().max()
    assert max_abs_delta < 1e-12


def test_daily_variant_weights_are_dollar_neutral_and_bounded():
    close = _panel()
    cfg = _cfg(gross_leverage=1.0)
    _, w = run_backtest_dollar_neutral_daily_variant(close, cfg)
    active_days = w[(w != 0).any(axis=1)]
    assert len(active_days) > 0
    row_sums = active_days.sum(axis=1)
    assert (row_sums.abs() < 1e-9).all()
    gross = active_days.abs().sum(axis=1)
    assert (gross <= cfg.gross_leverage + 1e-9).all()


def test_daily_variant_min_universe_floor_flattens_thin_days():
    close = _panel()
    cfg = _cfg(min_universe=len(UNIVERSE) + 1)
    _, w = run_backtest_dollar_neutral_daily_variant(close, cfg)
    assert (w == 0.0).all().all()


def test_default_universe_is_the_eleven_spdr_sector_etfs():
    assert set(DEFAULT_UNIVERSE) == {
        "XLE", "XLF", "XLK", "XLV", "XLI", "XLY", "XLP", "XLU", "XLB", "XLRE", "XLC",
    }
    assert len(DEFAULT_UNIVERSE) == 11
