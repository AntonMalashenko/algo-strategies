"""Gate 0 for S011 setup 1/6 (Double Seven): no-look-ahead + basic sanity.

No-look-ahead check: truncate the input at several points and confirm the
signal computed on the truncated series is byte-identical, for every bar up
to the truncation point, to the signal computed on the full series. If any
row used information from beyond its own timestamp, shortening the series
would change that row's value -- max|delta| over the overlap must be
exactly 0.0.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from strategies.double_seven import BASELINE_DOUBLE_SEVEN, DoubleSevenConfig, double_seven_signal
from utils.data import synthetic_ohlc


def test_no_lookahead_truncation():
    df = synthetic_ohlc(n=252 * 6, seed=11)
    full = double_seven_signal(df, BASELINE_DOUBLE_SEVEN)

    for cut in (300, 800, 1200, len(df) - 50):
        truncated = double_seven_signal(df.iloc[:cut], BASELINE_DOUBLE_SEVEN)
        overlap_full = full.iloc[:cut]
        max_abs_delta = (overlap_full - truncated).abs().max()
        assert max_abs_delta == 0.0, (
            f"look-ahead detected: cut={cut}, max|delta|={max_abs_delta}"
        )


def test_no_lookahead_short_config_window():
    """Same check with a non-default, short config -- window/trend_sma read
    from cfg, not hardcoded, so this also guards against a stray literal
    creeping into the engine."""
    df = synthetic_ohlc(n=252 * 3, seed=3)
    cfg = DoubleSevenConfig(window=4, trend_sma=50)
    full = double_seven_signal(df, cfg)
    for cut in (100, 400, len(df) - 20):
        truncated = double_seven_signal(df.iloc[:cut], cfg)
        max_abs_delta = (full.iloc[:cut] - truncated).abs().max()
        assert max_abs_delta == 0.0


def test_position_is_binary_and_starts_flat():
    df = synthetic_ohlc(n=252 * 2, seed=1)
    pos = double_seven_signal(df, BASELINE_DOUBLE_SEVEN)
    assert set(np.unique(pos.to_numpy())) <= {0.0, 1.0}
    # Before trend_sma has enough history, trend_ok can't be True -> must be flat.
    assert (pos.iloc[:BASELINE_DOUBLE_SEVEN.trend_sma - 1] == 0.0).all()


def test_exit_only_reachable_after_entry():
    """Sanity on the state machine itself: every 0->1 transition is an
    entry, every 1->0 transition is an exit; the position never "exits"
    while already flat (transitions strictly alternate)."""
    df = synthetic_ohlc(n=252 * 4, seed=42)
    pos = double_seven_signal(df, BASELINE_DOUBLE_SEVEN).to_numpy()
    changes = np.diff(pos)
    nonzero = changes[changes != 0]
    # Alternating +1 (entry) / -1 (exit), starting with +1 if any trade happens.
    if len(nonzero):
        assert nonzero[0] == 1.0
        for i in range(1, len(nonzero)):
            assert nonzero[i] == -nonzero[i - 1]
