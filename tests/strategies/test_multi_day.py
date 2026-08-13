"""Gate 0 for S011 setup 5/6 (Multiple Days Down): no-look-ahead + sanity."""
from __future__ import annotations

import numpy as np
import pandas as pd

from strategies.multi_day import ALL_MULTIDAY_PRESETS, compute_down_streak, multi_day_signal
from utils.data import synthetic_ohlc


def test_no_lookahead_all_presets():
    df = synthetic_ohlc(n=252 * 6, seed=11)
    for name, cfg in ALL_MULTIDAY_PRESETS.items():
        full = multi_day_signal(df, cfg)
        for cut in (300, 800, 1200, len(df) - 50):
            truncated = multi_day_signal(df.iloc[:cut], cfg)
            max_abs_delta = (full.iloc[:cut] - truncated).abs().max()
            assert max_abs_delta == 0.0, f"look-ahead in preset {name!r}: cut={cut}"


def test_down_streak_counts_correctly():
    close = pd.Series([100, 99, 98, 97, 98, 97, 96, 96, 95],
                       index=pd.date_range("2024-01-01", periods=9, freq="B"))
    # diffs:  nan  -1   -1   -1   +1   -1   -1    0   -1
    # streak:  0    1    2    3    0    1    2    0    1
    streak = compute_down_streak(close)
    assert list(streak.to_numpy()) == [0, 1, 2, 3, 0, 1, 2, 0, 1]


def test_higher_n_days_never_more_time_in_market_than_lower():
    """A stricter entry (more consecutive down days required) should never
    fire MORE often than a looser one on the same data -- sanity on the
    preset ordering."""
    df = synthetic_ohlc(n=252 * 8, seed=4)
    time_in_market = {name: multi_day_signal(df, cfg).mean() for name, cfg in ALL_MULTIDAY_PRESETS.items()}
    assert time_in_market["n3"] >= time_in_market["n4"] >= time_in_market["n5"]


def test_position_is_binary_and_starts_flat():
    df = synthetic_ohlc(n=252 * 2, seed=1)
    for cfg in ALL_MULTIDAY_PRESETS.values():
        pos = multi_day_signal(df, cfg)
        assert set(np.unique(pos.to_numpy())) <= {0.0, 1.0}
        assert (pos.iloc[:cfg.trend_sma - 1] == 0.0).all()
