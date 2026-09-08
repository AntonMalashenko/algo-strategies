"""Tests for strategies/xsect_combo.py — the S009+S012 blended book
(ALGODEV-13 combo decision, 2026-08-30).

What must hold:
  * the combo backtest is exactly portfolio_returns() over the weight-level
    blend of the two vol-off sleeve books (netting BEFORE costs);
  * forward_target_book() (the live path) reproduces the unscaled book the
    backtest itself would assign to the day after the panel ends — the same
    live==backtest guarantee bot/s009_paper.py::simulate() asserts for S009;
  * the frozen deploy in bot/s009_paper.py actually IS the recorded decision
    (30% momentum, 20%/yr portfolio vol target, 0.055%/side).
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from strategies.funding_carry import FundingCarryConfig
from strategies.funding_carry import run_backtest as run_carry
from strategies.xsect_combo import (
    COMBO_VOL_TARGET_ANNUAL,
    MOMENTUM_WEIGHT,
    XSectComboConfig,
    _backtest_book_for_next_day,
    blend_weights,
    forward_target_book,
    portfolio_returns,
    run_backtest,
)
from strategies.xsect_momentum import XSectMomentumConfig
from strategies.xsect_momentum import run_backtest as run_momentum

UNIVERSE = ("AAAUSDT", "BBBUSDT", "CCCUSDT", "DDDUSDT", "EEEUSDT", "FFFUSDT")


def _panels(n_days: int = 120, seed: int = 7):
    rng = np.random.default_rng(seed)
    days = pd.RangeIndex(20_000, 20_000 + n_days)
    rets = rng.normal(0.0, 0.03, size=(n_days, len(UNIVERSE)))
    close = pd.DataFrame(100.0 * np.exp(np.cumsum(rets, axis=0)), index=days, columns=UNIVERSE)
    funding = pd.DataFrame(rng.normal(0.0001, 0.0005, size=(n_days, len(UNIVERSE))),
                           index=days, columns=UNIVERSE)
    return close, funding


def _cfg(**changes) -> XSectComboConfig:
    base = XSectComboConfig(
        carry=FundingCarryConfig(signal_lookback_days=7, top_n=2, bottom_n=2,
                                 min_universe=4, universe=UNIVERSE),
        momentum=XSectMomentumConfig(lookback_days=14, top_n=2, bottom_n=2,
                                     min_universe=4, universe=UNIVERSE),
        taker_fee_per_side=0.00055,
    )
    return base.with_(**changes) if changes else base


def test_combo_equals_manual_blend_of_vol_off_sleeves():
    close, funding = _panels()
    cfg = _cfg(vol_target_annual=0.0)
    out, w = run_backtest(close, funding, cfg)

    _, w9 = run_carry(close, funding, cfg.carry)
    _, w12 = run_momentum(close, funding, cfg.momentum)
    w_manual = blend_weights(w9, w12, cfg.momentum_weight)
    price_ret = close.pct_change()
    valid = price_ret.notna() & funding.notna()
    out_manual, _ = portfolio_returns(w_manual, price_ret, funding, valid,
                                      taker_fee_per_side=cfg.taker_fee_per_side)
    pd.testing.assert_series_equal(out["net_ret"], out_manual["net_ret"])
    pd.testing.assert_frame_equal(w, w_manual)


def test_weight_level_blend_nets_turnover_before_costs():
    """Crossing legs must cancel in the book BEFORE the fee is applied, so the
    blended book's cost can never exceed the weight-sum of sleeve costs."""
    close, funding = _panels()
    cfg = _cfg(vol_target_annual=0.0)
    out, _ = run_backtest(close, funding, cfg)

    fee = cfg.taker_fee_per_side
    _, w9 = run_carry(close, funding, cfg.carry)
    _, w12 = run_momentum(close, funding, cfg.momentum)

    def _turnover(w):
        return w.diff().abs().sum(axis=1).fillna(w.abs().sum(axis=1))

    upper = (cfg.momentum_weight * _turnover(w12) + (1 - cfg.momentum_weight) * _turnover(w9)) * fee
    cost = out["gross_ret"] - out["net_ret"]
    assert (cost <= upper + 1e-12).all()
    assert cost.sum() < upper.sum()  # some netting must actually happen


def test_forward_book_matches_backtest_next_day_book():
    close, funding = _panels()
    cfg = _cfg(vol_target_annual=0.0)  # unscaled: selection logic only
    live = forward_target_book(close, funding, cfg)
    ref = _backtest_book_for_next_day(close, funding, cfg)
    assert live == ref
    assert live, "expected a non-empty book on a full synthetic panel"
    assert abs(sum(live.values())) < 1e-6  # dollar-neutral


def test_forward_book_vol_scale_is_bounded_and_past_only():
    close, funding = _panels()
    cfg = _cfg()  # vol target ON (decision default)
    book = forward_target_book(close, funding, cfg)
    unscaled = forward_target_book(close, funding, cfg.with_(vol_target_annual=0.0))
    assert set(book) == set(unscaled)
    ks = {s: book[s] / unscaled[s] for s in book}
    k = next(iter(ks.values()))
    assert all(abs(v - k) < 1e-3 for v in ks.values())  # one common scale
    assert 0.0 < k <= cfg.vol_scale_cap


def test_no_look_ahead_truncating_panel_keeps_past_days():
    close, funding = _panels()
    cfg = _cfg()
    out_full, _ = run_backtest(close, funding, cfg)
    cut = int(close.index[-10])
    out_cut, _ = run_backtest(close[close.index <= cut], funding[funding.index <= cut], cfg)
    common = [d for d in out_cut.index if d < cut]
    assert np.max(np.abs(out_full.loc[common, "net_ret"].values
                         - out_cut.loc[common, "net_ret"].values)) < 1e-12


def test_config_rejects_mismatched_universes_and_vol_on_sleeves():
    with pytest.raises(ValueError):
        XSectComboConfig(carry=FundingCarryConfig(universe=UNIVERSE),
                         momentum=XSectMomentumConfig(universe=UNIVERSE[:4]))
    with pytest.raises(ValueError):
        XSectComboConfig(carry=FundingCarryConfig(universe=UNIVERSE, vol_target_annual=0.2),
                         momentum=XSectMomentumConfig(universe=UNIVERSE))


def test_deploy_is_the_recorded_combo_decision():
    from bot.s009_paper import DEPLOY, DEPLOY_UNIVERSE
    assert isinstance(DEPLOY, XSectComboConfig)
    assert DEPLOY.momentum_weight == MOMENTUM_WEIGHT == 0.30
    assert DEPLOY.vol_target_annual == COMBO_VOL_TARGET_ANNUAL == 0.20
    assert DEPLOY.taker_fee_per_side == 0.00055
    assert DEPLOY.carry.signal_lookback_days == 7 and DEPLOY.carry.top_n == 2
    assert DEPLOY.momentum.lookback_days == 14 and DEPLOY.momentum.top_n == 3
    assert DEPLOY.universe == DEPLOY_UNIVERSE
    # sleeves vol-off is enforced by the config itself; assert anyway
    assert DEPLOY.carry.vol_target_annual == 0.0
    assert DEPLOY.momentum.vol_target_annual == 0.0
