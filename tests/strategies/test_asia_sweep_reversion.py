"""Gate 0 coverage for strategies/asia_sweep_reversion.py (S022.1): no-look-ahead,
Asia-range causality, sweep-and-reclaim signal geometry, SL/TP construction
(including the "nearest of two TP candidates" rule and its edge case), and
session/day bookkeeping.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from strategies.asia_sweep_reversion import (
    ASIA_SWEEP_BASE, AsiaSweepConfig, compute_asia_range, simulate, trades_to_frame,
)

BARS_PER_DAY = 288  # M5
START = "2025-01-06"  # a Monday; synthetic data ignores weekends anyway


def _pos(day: int, hh: int, mm: int = 0) -> int:
    """Bar position of day `day` (0-based) at hh:mm UTC, M5 grid."""
    return day * BARS_PER_DAY + hh * 12 + mm // 5


def _flat_bars(n_days: int, seed: int = 1, level: float = 1.1000, noise: float = 0.00002,
               wick: float = 0.00001) -> pd.DataFrame:
    """Mildly noisy flat M5 series around `level`, continuous tape (open = prior
    close). Callers inject explicit Asia-range/sweep events by overwriting
    specific bars' high/low/close afterward."""
    rng = np.random.default_rng(seed)
    n = n_days * BARS_PER_DAY
    idx = pd.date_range(START, periods=n, freq="5min")
    close = level + np.cumsum(rng.normal(0.0, noise, n))
    open_ = np.concatenate([[close[0]], close[:-1]])
    high = np.maximum(open_, close) + np.abs(rng.normal(0.0, wick, n))
    low = np.minimum(open_, close) - np.abs(rng.normal(0.0, wick, n))
    return pd.DataFrame(dict(open=open_, high=high, low=low, close=close), index=idx)


def _set_bar(df: pd.DataFrame, pos: int, high: float | None = None, low: float | None = None,
             close: float | None = None) -> None:
    """Overwrite one bar's high/low/close in place, keeping the next bar's open
    continuous with this bar's (possibly new) close."""
    if high is not None:
        df.iloc[pos, df.columns.get_loc("high")] = high
    if low is not None:
        df.iloc[pos, df.columns.get_loc("low")] = low
    if close is not None:
        df.iloc[pos, df.columns.get_loc("close")] = close
        if pos + 1 < len(df):
            df.iloc[pos + 1, df.columns.get_loc("open")] = close
    # keep OHLC internally consistent after overrides
    lo = min(df.iloc[pos][["open", "high", "low", "close"]])
    hi = max(df.iloc[pos][["open", "high", "low", "close"]])
    df.iloc[pos, df.columns.get_loc("high")] = max(hi, df.iloc[pos]["high"])
    df.iloc[pos, df.columns.get_loc("low")] = min(lo, df.iloc[pos]["low"])


def _asia_range_day(df: pd.DataFrame, day: int, hi: float, lo: float) -> None:
    """Pin day `day`'s Asia-window extremes to exactly (hi, lo) by placing one
    bar touching each, well inside the window, and flattening the rest of the
    window to stay strictly inside [lo, hi]."""
    mid = (hi + lo) / 2.0
    for hh, mm in ((0, 0), (1, 0), (2, 0), (5, 0), (6, 0), (6, 30)):
        p = _pos(day, hh, mm)
        _set_bar(df, p, high=mid + (hi - mid) * 0.1, low=mid - (mid - lo) * 0.1, close=mid)
    _set_bar(df, _pos(day, 3, 0), high=hi, low=mid, close=mid)
    _set_bar(df, _pos(day, 4, 0), high=mid, low=lo, close=mid)


def _gate0_series() -> tuple[pd.DataFrame, dict]:
    """Four days, each with a pinned Asia range and one deliberate event in the
    signal window: day0 sweep-long, day1 sweep-short, day2 no sweep (flat,
    stays inside range -> no trade), day3 sweep-long again (to test the
    one-trade-per-day / multi-day truncation checks)."""
    df = _flat_bars(5, seed=3)
    events = {}
    for day, (hi, lo) in ((0, (1.1015, 1.0990)), (1, (1.1015, 1.0990)),
                          (2, (1.1015, 1.0990)), (3, (1.1015, 1.0990)),
                          (4, (1.1015, 1.0990))):
        _asia_range_day(df, day, hi, lo)

    # day0: BUY -- sweep below asia_low then reclaim, at 08:00
    p = _pos(0, 8, 0)
    _set_bar(df, p, low=1.0985, close=1.0996, high=1.0998)
    events[0] = ("long", p)

    # day1: SELL -- sweep above asia_high then reclaim, at 07:30
    p = _pos(1, 7, 30)
    _set_bar(df, p, high=1.1020, close=1.1008, low=1.1005)
    events[1] = ("short", p)

    # day2: flat inside the range for EVERY bar of the signal window -- no trade
    # (every M5 bar from 07:00 to 09:25 inclusive is pinned, not just sample
    # points -- an un-pinned bar could inherit enough cumulative random-walk
    # drift from _flat_bars to spuriously pierce the tight range).
    for pos in range(_pos(2, 7, 0), _pos(2, 9, 30)):
        _set_bar(df, pos, high=1.1005, low=1.0998, close=1.1001)
    events[2] = (None, None)

    # day3: BUY again, later in the window (09:00), for a multi-day check
    p = _pos(3, 9, 0)
    _set_bar(df, p, low=1.0984, close=1.0997, high=1.0999)
    events[3] = ("long", p)

    # day4: flat inside the range for the whole signal window too -- a second
    # no-trade day, so the final trailing day of the fixture doesn't produce a
    # spurious trade from un-pinned residual noise.
    for pos in range(_pos(4, 7, 0), _pos(4, 9, 30)):
        _set_bar(df, pos, high=1.1005, low=1.0998, close=1.1001)
    events[4] = (None, None)

    return df, events


def _random_walk_with_spikes(n_days: int = 20, seed: int = 20260924) -> pd.DataFrame:
    """Seeded random walk, with an injected spike-and-reclaim wick near the
    start of the signal window on most days (alternating direction) so sweeps
    actually occur across a broad, less hand-tuned dataset."""
    df = _flat_bars(n_days, seed=seed, noise=0.00003, wick=0.000015)
    for day in range(n_days):
        hi = 1.1000 + 0.0004
        lo = 1.1000 - 0.0004
        _asia_range_day(df, day, hi, lo)
        if day % 3 == 2:
            continue  # leave some days flat (no sweep) for variety
        p = _pos(day, 7 + (day % 3), (day * 7) % 60 // 5 * 5)
        if day % 2 == 0:
            _set_bar(df, p, low=lo - 0.0006, close=lo + 0.0004, high=lo + 0.0006)
        else:
            _set_bar(df, p, high=hi + 0.0006, close=hi - 0.0004, low=hi - 0.0006)
    return df


# --------------------------------------------------------------------------- Gate 0: no look-ahead


def test_gate0_no_look_ahead_hand_built_series():
    df, events = _gate0_series()
    trades = trades_to_frame(simulate(df))
    assert len(trades) == 3  # day2 produced none
    assert list(trades["direction"]) == ["long", "short", "long"]

    for cutoff_day in (1, 2, 3, 4):
        cutoff = df.index[_pos(cutoff_day, 0, 0)]
        full = trades_to_frame(simulate(df))
        trunc = trades_to_frame(simulate(df.loc[df.index < cutoff]))
        f = full[full["entry_time"] < cutoff].reset_index(drop=True)
        t = trunc[trunc["entry_time"] < cutoff].reset_index(drop=True)
        pd.testing.assert_frame_equal(f, t, check_exact=True)


def test_gate0_no_look_ahead_random_walk():
    df = _random_walk_with_spikes()
    full = trades_to_frame(simulate(df))
    assert len(full) >= 5, "fixture should produce a reasonable number of trades"
    for day in (4, 8, 12, 16):
        cutoff = df.index[_pos(day, 0, 0)]
        trunc = trades_to_frame(simulate(df.loc[df.index < cutoff]))
        f = full[full["entry_time"] < cutoff].reset_index(drop=True)
        t = trunc[trunc["entry_time"] < cutoff].reset_index(drop=True)
        pd.testing.assert_frame_equal(f, t, check_exact=True)


def test_asia_range_is_causal_and_uses_only_its_own_window():
    """compute_asia_range for day D must equal the hand-computed max/min of the
    bars actually inside [asia_range_start, asia_range_end) on day D -- nothing
    from the signal window or the previous day leaks in."""
    df, _ = _gate0_series()
    rng = compute_asia_range(df)
    for day in range(4):
        d = df.index[_pos(day, 0, 0)].normalize()
        t = df.index.time
        window = df.loc[(df.index.normalize() == d) & (t >= ASIA_SWEEP_BASE.asia_range_start) &
                        (t < ASIA_SWEEP_BASE.asia_range_end)]
        assert rng.loc[d, "asia_high"] == pytest.approx(window["high"].max())
        assert rng.loc[d, "asia_low"] == pytest.approx(window["low"].min())


# --------------------------------------------------------------------------- entry geometry


def test_entry_is_next_bar_open_not_signal_bar_close():
    df, events = _gate0_series()
    trades = simulate(df)
    assert len(trades) == 3
    for t in trades:
        signal_pos = df.index.get_loc(t.signal_time)
        expected_entry_time = df.index[signal_pos + 1]
        assert t.entry_time == expected_entry_time
        assert t.entry_price == pytest.approx(df["open"].loc[expected_entry_time])


def test_sweep_long_and_short_signal_conditions():
    df, events = _gate0_series()
    trades = {t.day.day: t for t in simulate(df)}
    # day0 (Jan 6): long sweep at 08:00
    t0 = trades[6]
    assert t0.direction == "long"
    assert t0.sweep_extreme < t0.asia_low  # the sweep bar's low pierced below Asia_Low
    # day1 (Jan 7): short sweep
    t1 = trades[7]
    assert t1.direction == "short"
    assert t1.sweep_extreme > t1.asia_high


def test_no_sweep_no_trade_day():
    """Day2 stays strictly inside the Asia range for the whole signal window
    -- must produce zero trades that day."""
    df, events = _gate0_series()
    trades = simulate(df)
    days_traded = {t.day.normalize() for t in trades}
    day2 = df.index[_pos(2, 0, 0)].normalize()
    assert day2 not in days_traded


def test_one_trade_per_day_max():
    df = _random_walk_with_spikes(n_days=15, seed=99)
    trades = simulate(df)
    days = [t.day for t in trades]
    assert len(days) == len(set(days))


# --------------------------------------------------------------------------- SL / TP construction


def test_stop_beyond_sweep_extreme_plus_buffer():
    df, _ = _gate0_series()
    trades = simulate(df)
    for t in trades:
        if t.direction == "long":
            assert t.stop_price == pytest.approx(t.sweep_extreme - ASIA_SWEEP_BASE.sweep_buffer_price)
            assert t.stop_price < t.entry_price
        else:
            assert t.stop_price == pytest.approx(t.sweep_extreme + ASIA_SWEEP_BASE.sweep_buffer_price)
            assert t.stop_price > t.entry_price


def test_tp_picks_the_nearer_of_opposite_boundary_or_r_multiple():
    """Construct one case where the opposite boundary is the nearer target
    (tp_candidate == 'opposite_boundary') and one where 1.5xSL is nearer
    ('r_mult'), by varying how wide the Asia range is relative to the stop
    distance -- both branches of the "nearest of two" rule must be reachable
    and correctly labeled."""
    df = _flat_bars(2, seed=5)
    # Wide Asia range (far opposite boundary) -> 1.5xSL should win (nearer).
    _asia_range_day(df, 0, hi=1.1300, lo=1.0700)  # very wide
    p_wide = _pos(0, 8, 0)
    _set_bar(df, p_wide, low=1.0695, close=1.0705, high=1.0707)  # small sweep -> small SL -> r_mult nearer
    # Narrow Asia range (near opposite boundary) -> boundary should win (nearer).
    df2 = _flat_bars(2, seed=6)
    _asia_range_day(df2, 0, hi=1.1010, lo=1.0990)
    p_narrow = _pos(0, 8, 0)
    _set_bar(df2, p_narrow, low=1.0940, close=1.0996, high=1.0998)  # deep sweep -> big SL -> boundary nearer

    t_wide = simulate(df)
    t_narrow = simulate(df2)
    assert len(t_wide) == 1 and len(t_narrow) == 1
    assert t_wide[0].tp_candidate == "r_mult"
    assert t_narrow[0].tp_candidate == "opposite_boundary"
    assert t_narrow[0].tp_price == pytest.approx(t_narrow[0].asia_high)
    assert t_wide[0].tp_price == pytest.approx(
        t_wide[0].entry_price + ASIA_SWEEP_BASE.tp_r_mult * t_wide[0].stop_dist)


def test_same_bar_sl_and_tp_resolves_as_stop():
    df, _ = _gate0_series()
    trades = simulate(df)
    t = next(t for t in trades if t.direction == "long")
    entry_pos = df.index.get_loc(t.entry_time)
    nxt = entry_pos + 1
    df.iloc[nxt, df.columns.get_loc("high")] = 1.2000
    df.iloc[nxt, df.columns.get_loc("low")] = 1.0000
    trades2 = simulate(df)
    t2 = next(x for x in trades2 if x.signal_time == t.signal_time)
    assert t2.exit_reason == "stop"
    assert t2.exit_time == df.index[nxt]


def test_forced_time_exit_at_16_00_utc():
    df, _ = _gate0_series()
    trades = simulate(df)
    time_exits = [t for t in trades if t.exit_reason == "time"]
    for t in time_exits:
        assert t.exit_time.time() < ASIA_SWEEP_BASE.force_close


def test_empty_input_returns_no_trades():
    empty = pd.DataFrame(columns=["open", "high", "low", "close"])
    assert simulate(empty, ASIA_SWEEP_BASE) == []
