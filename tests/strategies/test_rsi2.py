"""Gate 0 for S011 setup 2/6 (RSI(2)): no-look-ahead + basic sanity, all presets."""
from __future__ import annotations

import numpy as np
import pandas as pd

from strategies.rsi2 import ALL_RSI2_PRESETS, rsi2_signal, wilder_rsi
from utils.data import synthetic_ohlc


def test_no_lookahead_all_presets():
    df = synthetic_ohlc(n=252 * 6, seed=11)
    for name, cfg in ALL_RSI2_PRESETS.items():
        full = rsi2_signal(df, cfg)
        for cut in (300, 800, 1200, len(df) - 50):
            truncated = rsi2_signal(df.iloc[:cut], cfg)
            max_abs_delta = (full.iloc[:cut] - truncated).abs().max()
            assert max_abs_delta == 0.0, f"look-ahead in preset {name!r}: cut={cut}"


def test_wilder_rsi_bounds():
    df = synthetic_ohlc(n=252 * 3, seed=5)
    rsi = wilder_rsi(df["close"], 2)
    valid = rsi.dropna()
    assert (valid >= 0.0).all() and (valid <= 100.0).all()


def test_wilder_rsi_monotone_all_gains_all_losses():
    """A strictly rising series should push RSI to 100 (no losses at all);
    a strictly falling series should push it to 0 (no gains at all) --
    sanity on the edge-case handling in wilder_rsi's divide-by-zero paths."""
    idx = pd.date_range("2020-01-01", periods=20, freq="B")
    rising = pd.Series(np.linspace(100, 120, 20), index=idx)
    falling = pd.Series(np.linspace(120, 100, 20), index=idx)
    rsi_up = wilder_rsi(rising, 2).dropna()
    rsi_down = wilder_rsi(falling, 2).dropna()
    assert (rsi_up == 100.0).all()
    assert (rsi_down == 0.0).all()


def test_position_is_binary_and_starts_flat():
    df = synthetic_ohlc(n=252 * 2, seed=1)
    for cfg in ALL_RSI2_PRESETS.values():
        pos = rsi2_signal(df, cfg)
        assert set(np.unique(pos.to_numpy())) <= {0.0, 1.0}
        assert (pos.iloc[:cfg.trend_sma - 1] == 0.0).all()
