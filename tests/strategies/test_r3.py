"""Gate 0 for S011 setup 6/6 (R3, reconstructed): no-look-ahead + sanity."""
from __future__ import annotations

import numpy as np
import pandas as pd

from strategies.r3 import ALL_R3_PRESETS, compute_lower_low_streak, r3_signal
from utils.data import synthetic_ohlc


def test_no_lookahead_all_presets():
    df = synthetic_ohlc(n=252 * 6, seed=11)
    for name, cfg in ALL_R3_PRESETS.items():
        full = r3_signal(df, cfg)
        for cut in (300, 800, 1200, len(df) - 50):
            truncated = r3_signal(df.iloc[:cut], cfg)
            max_abs_delta = (full.iloc[:cut] - truncated).abs().max()
            assert max_abs_delta == 0.0, f"look-ahead in preset {name!r}: cut={cut}"


def test_lower_low_streak_counts_correctly():
    low = pd.Series([50, 49, 48, 49, 47, 46, 46],
                     index=pd.date_range("2024-01-01", periods=7, freq="B"))
    # diffs:   nan  -1   -1   +1   -2   -1    0
    # streak:   0    1    2    0    1    2    0
    streak = compute_lower_low_streak(low)
    assert list(streak.to_numpy()) == [0, 1, 2, 0, 1, 2, 0]


def test_no_gap_filter_never_stricter_than_gap_capitulation():
    """gap_capitulation ANDs an extra gap-down condition onto the same
    lower-low streak -- its entry days must be a subset of no_gap_filter's,
    so it can never spend MORE time in market."""
    df = synthetic_ohlc(n=252 * 8, seed=4)
    with_gap = r3_signal(df, ALL_R3_PRESETS["gap_capitulation"]).mean()
    without_gap = r3_signal(df, ALL_R3_PRESETS["no_gap_filter"]).mean()
    assert with_gap <= without_gap


def test_position_is_binary_and_starts_flat():
    df = synthetic_ohlc(n=252 * 2, seed=1)
    for cfg in ALL_R3_PRESETS.values():
        pos = r3_signal(df, cfg)
        assert set(np.unique(pos.to_numpy())) <= {0.0, 1.0}
        assert (pos.iloc[:cfg.trend_sma - 1] == 0.0).all()
