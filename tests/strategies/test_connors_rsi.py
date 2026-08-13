"""Gate 0 for S011 setup 3/6 (ConnorsRSI): no-look-ahead + component sanity."""
from __future__ import annotations

import numpy as np
import pandas as pd

from strategies.connors_rsi import (
    ALL_CONNORS_RSI_PRESETS,
    compute_streak,
    connors_rsi_signal,
    percent_rank,
)
from utils.data import synthetic_ohlc


def test_no_lookahead_all_presets():
    df = synthetic_ohlc(n=252 * 6, seed=11)
    for name, cfg in ALL_CONNORS_RSI_PRESETS.items():
        full = connors_rsi_signal(df, cfg)
        for cut in (300, 800, 1200, len(df) - 50):
            truncated = connors_rsi_signal(df.iloc[:cut], cfg)
            max_abs_delta = (full.iloc[:cut] - truncated).abs().max()
            assert max_abs_delta == 0.0, f"look-ahead in preset {name!r}: cut={cut}"


def test_streak_resets_and_signs():
    close = pd.Series([100, 101, 102, 101, 100, 99, 99, 100],
                       index=pd.date_range("2024-01-01", periods=8, freq="B"))
    streak = compute_streak(close)
    # idx: 0     1    2    3     4     5     6    7
    # close:100  101  102  101   100   99    99   100
    # diff:  nan  +1   +1   -1    -1    -1    0    +1
    expected = [0, 1, 2, -1, -2, -3, 0, 1]
    assert list(streak.to_numpy()) == expected


def test_percent_rank_bounds_and_extremes():
    idx = pd.date_range("2020-01-01", periods=150, freq="B")
    rising = pd.Series(np.linspace(1, 150, 150), index=idx)  # monotonically increasing "returns"
    rank = percent_rank(rising, period=100).dropna()
    # a monotonically increasing series: today's value is always the max of
    # its trailing window -> percent rank should be 100 (<=today's value is
    # the whole window).
    assert (rank == 100.0).all()


def test_position_is_binary_and_starts_flat():
    df = synthetic_ohlc(n=252 * 2, seed=1)
    for cfg in ALL_CONNORS_RSI_PRESETS.values():
        pos = connors_rsi_signal(df, cfg)
        assert set(np.unique(pos.to_numpy())) <= {0.0, 1.0}
        assert (pos.iloc[:cfg.trend_sma - 1] == 0.0).all()
