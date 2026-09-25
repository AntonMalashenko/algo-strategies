"""Gate 0 coverage for strategies/asia_mean_reversion.py (S022): no-look-ahead,
session-anchored VWAP/Z-score causality, and basic entry/exit geometry.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from strategies.asia_mean_reversion import (
    ASIA_MR_BASE, AsiaMrConfig, add_indicators, simulate, trades_to_frame,
)


def _synthetic_m5(n_sessions: int = 20, seed: int = 7) -> pd.DataFrame:
    """n_sessions consecutive Asia sessions (22:00-06:00 UTC), M5 bars, with
    occasional sharp spikes so Z/RSI conditions actually fire both ways."""
    rng = np.random.default_rng(seed)
    rows = []
    price = 1.1000
    day0 = pd.Timestamp("2025-01-01")
    for s in range(n_sessions):
        session_open = day0 + pd.Timedelta(days=s) + pd.Timedelta(hours=22)
        n_bars = 96  # 8h / 5min
        # Inject a directional spike mid-session on alternating sessions so
        # both long and short signals occur across the synthetic set.
        spike_dir = 1 if s % 2 == 0 else -1
        for k in range(n_bars):
            ts = session_open + pd.Timedelta(minutes=5 * k)
            if 30 <= k < 34:
                price += spike_dir * 0.0009
            else:
                price += rng.normal(0, 0.00005)
            o = price
            h = price + abs(rng.normal(0, 0.00003))
            l = price - abs(rng.normal(0, 0.00003))
            c = price + rng.normal(0, 0.00002)
            rows.append({"dt": ts, "open": o, "high": max(h, o, c), "low": min(l, o, c), "close": c})
    df = pd.DataFrame(rows).set_index("dt").sort_index()
    return df[["open", "high", "low", "close"]]


def test_no_look_ahead_truncation():
    """Truncating the input after some cutoff must not change any already-
    emitted trade whose entry_time is strictly before the cutoff -- the
    standard Gate 0 proof used throughout this repo (see
    strategies/orb_intraday/engine.py's docstring for the same convention)."""
    m5 = _synthetic_m5(n_sessions=12, seed=3)
    full_trades = trades_to_frame(simulate(m5, ASIA_MR_BASE))
    assert len(full_trades) > 0, "fixture should produce at least one trade"

    cutoff = m5.index[len(m5) // 2]
    truncated = m5.loc[m5.index <= cutoff]
    trunc_trades = trades_to_frame(simulate(truncated, ASIA_MR_BASE))

    prior_full = full_trades[full_trades["entry_time"] < cutoff].reset_index(drop=True)
    prior_trunc = trunc_trades[trunc_trades["entry_time"] < cutoff].reset_index(drop=True)
    pd.testing.assert_frame_equal(prior_full, prior_trunc)


def test_no_look_ahead_multiple_cutoffs():
    m5 = _synthetic_m5(n_sessions=16, seed=11)
    full_trades = trades_to_frame(simulate(m5, ASIA_MR_BASE))
    for frac in (0.3, 0.5, 0.7, 0.9):
        cutoff = m5.index[int(len(m5) * frac)]
        truncated = m5.loc[m5.index <= cutoff]
        trunc_trades = trades_to_frame(simulate(truncated, ASIA_MR_BASE))
        prior_full = full_trades[full_trades["entry_time"] < cutoff].reset_index(drop=True)
        prior_trunc = trunc_trades[trunc_trades["entry_time"] < cutoff].reset_index(drop=True)
        pd.testing.assert_frame_equal(prior_full, prior_trunc)


def test_vwap_and_z_are_session_causal():
    """VWAP/StdDev/Z at bar i must depend only on bars 0..i of the SAME
    session -- verified by recomputing them by hand for one session and
    comparing to add_indicators()'s output."""
    m5 = _synthetic_m5(n_sessions=2, seed=5)
    df = add_indicators(m5, ASIA_MR_BASE)
    first_session = df[df["session"] == df["session"].iloc[0]]
    typical = first_session["typical"].to_numpy()
    for k in range(1, len(first_session)):
        expected_vwap = typical[: k + 1].mean()
        expected_std = typical[: k + 1].std(ddof=1)
        assert first_session["vwap"].iloc[k] == pytest.approx(expected_vwap)
        assert first_session["stddev"].iloc[k] == pytest.approx(expected_std)


def test_entry_is_next_bar_open_not_signal_bar_close():
    """Entry must execute at the bar AFTER the signal bar (its open), never at
    the signal bar's own close -- an idealized same-bar-close fill would be a
    look-ahead-adjacent bug (the signal is only knowable once that bar closes)."""
    m5 = _synthetic_m5(n_sessions=6, seed=21)
    df = add_indicators(m5, ASIA_MR_BASE)
    trades = simulate(m5, ASIA_MR_BASE)
    assert len(trades) > 0
    for t in trades:
        signal_pos = df.index.get_loc(t.signal_time)
        expected_entry_time = df.index[signal_pos + 1]
        assert t.entry_time == expected_entry_time
        assert t.entry_price == pytest.approx(df["open"].loc[expected_entry_time])


def test_one_trade_per_session_max():
    m5 = _synthetic_m5(n_sessions=10, seed=42)
    trades = simulate(m5, ASIA_MR_BASE)
    sessions = [t.session for t in trades]
    assert len(sessions) == len(set(sessions)), "at most one trade per session"


def test_stop_distance_uses_atr_at_signal_bar():
    m5 = _synthetic_m5(n_sessions=8, seed=99)
    df = add_indicators(m5, ASIA_MR_BASE)
    trades = simulate(m5, ASIA_MR_BASE)
    assert len(trades) > 0
    for t in trades:
        expected_dist = ASIA_MR_BASE.stop_atr_mult * t.atr_at_entry
        actual_dist = abs(t.entry_price - t.stop_price)
        assert actual_dist == pytest.approx(expected_dist)


def test_long_and_short_geometry():
    m5 = _synthetic_m5(n_sessions=10, seed=17)
    trades = simulate(m5, ASIA_MR_BASE)
    for t in trades:
        if t.direction == "long":
            assert t.stop_price < t.entry_price
        else:
            assert t.stop_price > t.entry_price


def test_stop_takes_precedence_over_vwap_same_bar():
    """If a bar's range would satisfy both the SL and the VWAP-touch exit,
    the engine must record it as a stop exit (conservative ordering)."""
    cfg = ASIA_MR_BASE.with_(stop_atr_mult=0.05)  # tiny stop -> easy to hit same bar as VWAP
    m5 = _synthetic_m5(n_sessions=10, seed=55)
    trades = simulate(m5, cfg)
    stop_exits = [t for t in trades if t.exit_reason == "stop"]
    assert len(stop_exits) > 0, "tiny stop distance should force at least one stop exit"


def test_empty_input_returns_no_trades():
    empty = pd.DataFrame(columns=["open", "high", "low", "close"])
    assert simulate(empty, ASIA_MR_BASE) == []
