"""Gate 0 for S011 setup 4/6 (RSI4): no-look-ahead + basic sanity, both presets."""
from __future__ import annotations

import numpy as np

from strategies.rsi4 import ALL_RSI4_PRESETS, rsi4_signal
from utils.data import synthetic_ohlc


def test_no_lookahead_all_presets():
    df = synthetic_ohlc(n=252 * 6, seed=11)
    for name, cfg in ALL_RSI4_PRESETS.items():
        full = rsi4_signal(df, cfg)
        for cut in (300, 800, 1200, len(df) - 50):
            truncated = rsi4_signal(df.iloc[:cut], cfg)
            max_abs_delta = (full.iloc[:cut] - truncated).abs().max()
            assert max_abs_delta == 0.0, f"look-ahead in preset {name!r}: cut={cut}"


def test_time_stop_exits_on_schedule():
    """The time_stop preset must never hold a position longer than
    time_stop_days consecutive decided-1.0 bars."""
    df = synthetic_ohlc(n=252 * 4, seed=21)
    cfg = ALL_RSI4_PRESETS["time_stop"]
    pos = rsi4_signal(df, cfg).to_numpy()

    run_length = 0
    max_run = 0
    for v in pos:
        if v == 1.0:
            run_length += 1
            max_run = max(max_run, run_length)
        else:
            run_length = 0
    assert max_run <= cfg.time_stop_days, (
        f"time_stop preset held a position for {max_run} bars, "
        f"exceeding configured time_stop_days={cfg.time_stop_days}"
    )


def test_position_is_binary_and_starts_flat():
    df = synthetic_ohlc(n=252 * 2, seed=1)
    for cfg in ALL_RSI4_PRESETS.values():
        pos = rsi4_signal(df, cfg)
        assert set(np.unique(pos.to_numpy())) <= {0.0, 1.0}
        assert (pos.iloc[:cfg.trend_sma - 1] == 0.0).all()
