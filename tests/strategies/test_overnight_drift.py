"""Gate 0 for S025 (US Index Overnight Drift): no-look-ahead + decomposition sanity."""
from __future__ import annotations

import numpy as np
import pandas as pd

from strategies.overnight_drift import (
    diagnose_open_data_quality,
    annual_breakdown,
    buy_and_hold_equity_curve,
    decompose_overnight_intraday,
    gate0_no_look_ahead,
    intraday_only_equity_curve,
    overnight_equity_curve,
)
from utils.data import synthetic_ohlc


def test_no_lookahead():
    df = synthetic_ohlc(n=252 * 6, seed=25)
    max_abs_delta = gate0_no_look_ahead(df, cut_days=200)
    assert max_abs_delta == 0.0


def test_no_lookahead_multiple_cuts():
    df = synthetic_ohlc(n=252 * 6, seed=25)
    full = decompose_overnight_intraday(df)["overnight_ret"]
    for cut in (300, 800, 1200, len(df) - 50):
        truncated = decompose_overnight_intraday(df.iloc[:cut])["overnight_ret"]
        common = full.index.intersection(truncated.index)
        max_abs_delta = (full.loc[common] - truncated.loc[common]).abs().max()
        assert max_abs_delta == 0.0, f"look-ahead: cut={cut}"


def test_overnight_plus_intraday_compounds_to_full_day():
    """The whole premise of the module is a decomposition, not two independent
    series -- (1+overnight)*(1+intraday) must equal (1+full_day) exactly, every
    single day, or the split has leaked/lost return somewhere."""
    df = synthetic_ohlc(n=252 * 4, seed=3)
    legs = decompose_overnight_intraday(df)
    reconstructed = (1.0 + legs["overnight_ret"]) * (1.0 + legs["intraday_ret"]) - 1.0
    max_abs_delta = (reconstructed - legs["full_day_ret"]).abs().max()
    assert max_abs_delta < 1e-12


def test_first_row_dropped_no_nan():
    df = synthetic_ohlc(n=252, seed=1)
    legs = decompose_overnight_intraday(df)
    assert len(legs) == len(df) - 1
    assert not legs.isna().any().any()


def test_overnight_equity_curve_frictionless_matches_gross_compounding():
    df = synthetic_ohlc(n=252 * 2, seed=9)
    equity = overnight_equity_curve(df, cost_bps_per_side=0.0, start_equity=1.0)
    gross = decompose_overnight_intraday(df)["overnight_ret"]
    expected = (1.0 + gross).cumprod()
    max_abs_delta = (equity.to_numpy() - expected.to_numpy()).__abs__().max()
    assert max_abs_delta < 1e-12


def test_cost_strictly_reduces_equity_vs_frictionless():
    df = synthetic_ohlc(n=252 * 2, seed=9)
    gross_equity = overnight_equity_curve(df, cost_bps_per_side=0.0)
    net_equity = overnight_equity_curve(df, cost_bps_per_side=2.0)
    assert net_equity.iloc[-1] < gross_equity.iloc[-1]


def test_equity_curves_start_positive_and_finite():
    df = synthetic_ohlc(n=252 * 2, seed=9)
    for curve in (
        overnight_equity_curve(df),
        buy_and_hold_equity_curve(df),
        intraday_only_equity_curve(df),
    ):
        assert np.isfinite(curve.to_numpy()).all()
        assert (curve > 0).all()


def test_annual_breakdown_covers_every_year_in_range():
    df = synthetic_ohlc(n=252 * 5, seed=42)
    breakdown = annual_breakdown(df)
    legs = decompose_overnight_intraday(df)
    expected_years = sorted(set(legs.index.year))
    assert list(breakdown.index) == expected_years
    assert (breakdown["n_days"] > 0).all()
    assert breakdown["overnight_hit_rate"].between(0.0, 1.0).all()
    assert breakdown["intraday_hit_rate"].between(0.0, 1.0).all()


def test_diagnose_open_data_quality_flags_degenerate_open():
    """synthetic_ohlc() sets open[t] = close[t-1] exactly (utils/data.py), which is
    coincidentally the SAME artifact this diagnostic exists to catch in the real
    vendor CSVs -- so it must report ~100% contamination and no reliable date."""
    df = synthetic_ohlc(n=252 * 3, seed=7)
    report = diagnose_open_data_quality(df)
    assert report["frac_zero_overnight"] > 0.99
    assert report["first_reliable_date"] is None


def test_diagnose_open_data_quality_clears_once_open_is_independent():
    """Splice real open/close jitter onto the back half of an otherwise-degenerate
    frame and confirm the diagnostic actually distinguishes it -- not just always
    reporting contamination regardless of the data."""
    df = synthetic_ohlc(n=252 * 4, seed=7)
    rng = np.random.default_rng(0)
    half = len(df) // 2
    jitter = rng.normal(0, 0.003, len(df) - half)
    df = df.copy()
    df.iloc[half:, df.columns.get_loc("open")] = (
        df["close"].shift(1).iloc[half:].to_numpy() * (1.0 + jitter)
    )
    report = diagnose_open_data_quality(df)
    assert report["frac_zero_overnight"] < 0.55
    assert report["first_reliable_date"] is not None
    assert report["first_reliable_date"] >= df.index[half]
