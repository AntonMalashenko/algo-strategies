"""Gate 0 coverage for strategies/gold_chandelier_breakout.py (S024.1): no-look-
ahead, London-range causality, EMA(200) filter, entry/session geometry, and --
the highest-risk area per the prompt -- a dedicated adversarial proof that the
Chandelier trailing-stop update/check ordering is strictly causal (never uses a
bar's own favorable extreme to justify stopping that SAME bar out).
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from strategies.gold_chandelier_breakout import (
    GOLD_CHANDELIER_BASE, GoldChandelierConfig, add_indicators, compute_london_range,
    simulate, trades_to_frame, walk_chandelier_exit,
)

BARS_PER_DAY = 96          # M15
START = "2024-01-02"       # a Tuesday; synthetic data ignores weekends anyway
OSC_PERIOD = 10
WICK = 0.2


def _pos(day: int, hh: int, mm: int = 0) -> int:
    return day * BARS_PER_DAY + hh * 4 + mm // 15


def _bars(n_days: int, level_jumps: dict[int, float] | None = None,
          amplitude: float = 0.3) -> pd.DataFrame:
    """Sine-oscillation M15 series (same construction as
    strategies/gold_session_momentum.py's test fixture) -- low amplitude so a
    level jump reliably produces a clean breakout of the London range."""
    n = n_days * BARS_PER_DAY
    idx = pd.date_range(START, periods=n, freq="15min")
    level = np.full(n, 2000.0)
    for pos, delta in (level_jumps or {}).items():
        level[pos:] += delta
    close = level + amplitude * np.sin(2 * np.pi * np.arange(n) / OSC_PERIOD)
    open_ = np.concatenate([[close[0]], close[:-1]])
    high = np.maximum(open_, close) + WICK
    low = np.minimum(open_, close) - WICK
    return pd.DataFrame(dict(open=open_, high=high, low=low, close=close), index=idx)


def _gate0_series() -> pd.DataFrame:
    """Warm-up EMA200 for the first couple of days (level flat), then
    deliberate up/down breakouts in the entry window on later days."""
    jumps = {
        _pos(3, 13): +6.0,    # day3 13:00: up-breakout, above EMA -> long
        _pos(4, 14): -14.0,   # day4 14:00: down-breakout, below EMA -> short
    }
    return _bars(6, jumps)


def _random_walk_series(n_days: int = 20, seed: int = 20260925) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    n = n_days * BARS_PER_DAY
    vol = 0.5 + 0.4 * np.abs(np.sin(np.arange(n) / 37.0))
    close = 2000.0 + np.cumsum(rng.normal(0.0, 1.0, n) * vol)
    open_ = np.concatenate([[close[0]], close[:-1]])
    high = np.maximum(open_, close) + np.abs(rng.normal(0.0, 0.5, n)) * vol
    low = np.minimum(open_, close) - np.abs(rng.normal(0.0, 0.5, n)) * vol
    idx = pd.date_range(START, periods=n, freq="15min")
    return pd.DataFrame(dict(open=open_, high=high, low=low, close=close), index=idx)


# --------------------------------------------------------------------------- Gate 0: no look-ahead


def _assert_prefix_identical(full: pd.DataFrame, cutoff: pd.Timestamp) -> int:
    ft = trades_to_frame(simulate(full))
    tt = trades_to_frame(simulate(full.loc[full.index < cutoff]))
    ft = ft[ft["entry_time"] < cutoff].reset_index(drop=True)
    tt = tt[tt["entry_time"] < cutoff].reset_index(drop=True)
    pd.testing.assert_frame_equal(ft, tt, check_exact=True)
    return len(ft)


def test_gate0_no_look_ahead_hand_built_series():
    full = _gate0_series()
    all_trades = trades_to_frame(simulate(full))
    assert len(all_trades) == 2
    assert list(all_trades["direction"]) == ["long", "short"]
    for day in (4, 5):
        cutoff = full.index[_pos(day, 0)]
        _assert_prefix_identical(full, cutoff)


def test_gate0_no_look_ahead_random_walk():
    full = _random_walk_series()
    all_trades = trades_to_frame(simulate(full))
    assert len(all_trades) >= 5
    for day in (4, 7, 10, 13, 16, 19):
        _assert_prefix_identical(full, full.index[_pos(day, 0)])


def test_indicators_are_causal_under_truncation():
    full = _random_walk_series()
    cutoff = 700
    a = add_indicators(full)
    b = add_indicators(full.iloc[:cutoff])
    pd.testing.assert_frame_equal(a.iloc[:cutoff][["ema", "atr"]], b[["ema", "atr"]], check_exact=True)


def test_london_range_is_causal_and_uses_only_its_own_window():
    full = _gate0_series()
    rng = compute_london_range(full)
    t = full.index.time
    for day in range(6):
        d = full.index[_pos(day, 0)].normalize()
        window = full.loc[(full.index.normalize() == d) & (t >= GOLD_CHANDELIER_BASE.london_start) &
                          (t < GOLD_CHANDELIER_BASE.london_end)]
        assert rng.loc[d, "london_high"] == pytest.approx(window["high"].max())
        assert rng.loc[d, "london_low"] == pytest.approx(window["low"].min())


# --------------------------------------------------------------------------- entry / filter geometry


def test_breakout_and_ema_filter_geometry():
    full = _gate0_series()
    trades = simulate(full)
    assert len(trades) == 2
    t_long = next(t for t in trades if t.direction == "long")
    t_short = next(t for t in trades if t.direction == "short")
    assert t_long.entry_price > t_long.london_high and t_long.entry_price > t_long.ema_at_entry
    assert t_short.entry_price < t_short.london_low and t_short.entry_price < t_short.ema_at_entry


def test_entry_price_is_signal_bar_close():
    full = _gate0_series()
    trades = simulate(full)
    for t in trades:
        assert t.entry_price == pytest.approx(full["close"].loc[t.signal_time])
        assert t.entry_time == t.signal_time + pd.Timedelta(minutes=GOLD_CHANDELIER_BASE.bar_minutes)


def test_one_trade_per_day_max():
    full = _random_walk_series(n_days=15, seed=7)
    trades = simulate(full)
    days = [t.day for t in trades]
    assert len(days) == len(set(days))


def test_out_of_window_breakout_is_not_an_entry():
    """A close beyond the London range at 06:00 (before the 13:00 entry window
    even opens) must not be traded."""
    df = _bars(4, {_pos(3, 6): +6.0})
    trades = simulate(df)
    assert all(t.signal_time.time() >= GOLD_CHANDELIER_BASE.entry_start for t in trades)


def test_initial_stop_size_and_no_trail_case():
    """A trade that never reaches the 1.0xATR profit trigger must be stopped
    (or time-exited) using the ORIGINAL 1.5xATR stop, trail_armed False."""
    df = _bars(4, {_pos(3, 13): +6.0}, amplitude=0.05)  # tiny oscillation after entry -> never triggers trail
    ind = add_indicators(df)
    trades = simulate(df)
    assert len(trades) == 1
    t = trades[0]
    atr = ind["atr"].loc[t.signal_time]
    assert t.atr_at_entry == pytest.approx(atr)
    assert t.stop_dist == pytest.approx(GOLD_CHANDELIER_BASE.stop_atr_mult * atr)
    assert t.initial_stop_price == pytest.approx(t.entry_price - 1.5 * atr)
    assert t.trail_armed is False
    assert t.exit_reason in ("time", "stop")


def test_empty_input_returns_no_trades():
    empty = pd.DataFrame(columns=["open", "high", "low", "close"])
    assert simulate(empty, GOLD_CHANDELIER_BASE) == []


# --------------------------------------------------------------------------- Chandelier causality (critical)
#
# These tests call walk_chandelier_exit() DIRECTLY -- the one function
# responsible for the causality guarantee -- rather than going through the
# full simulate() pipeline, so the post-entry bar sequence can be fully
# controlled without also having to engineer a valid London-range breakout.


def _walk_fixture(bars: list[tuple[float, float, float]], entry_price: float = 100.0,
                  atr_at_entry: float = 2.0, direction: str = "long",
                  cfg: GoldChandelierConfig = GOLD_CHANDELIER_BASE):
    """bars: list of (high, low, close) for consecutive M15 bars starting at
    start_pos, all on the same UTC day, all before cfg.force_close."""
    idx = pd.date_range("2024-03-04 13:15", periods=len(bars), freq="15min")
    high = np.array([b[0] for b in bars])
    low = np.array([b[1] for b in bars])
    close = np.array([b[2] for b in bars])
    t = idx.time
    days = idx.normalize()
    day = days[0]
    return walk_chandelier_exit(idx, high, low, close, t, days, 0, day, direction,
                                entry_price, atr_at_entry, cfg)


def test_chandelier_stop_uses_only_prior_bars_never_the_current_bars_own_extreme():
    """entry_price=100, atr_at_entry=2.0 -> stop_dist=3.0 (initial stop=97),
    trigger_dist=2.0 (arm once extreme>=102), trail_dist=4.0.

    Bar A: high=101, low=99.5 -- uneventful, doesn't arm (101-100=1<2), stop
      stays 97.
    Bar B: high=103, low=98 -- crosses the trigger (103-100=3>=2) and would
      ratchet the stop to 103-4=99. Bar B's own low (98) is BELOW that new
      stop (99) but ABOVE the old stop (97).
        * Buggy (update-then-check-same-bar): arms/ratchets FIRST using bar
          B's high, then checks bar B's own low (98) against the NEW stop
          (99) -> incorrectly stops out ON BAR B at 99.
        * Correct (causal): checks bar B's low (98) against the OLD stop (97)
          first -> 98 > 97, NOT stopped; only then updates extreme/arms/
          ratchets for bar C onward.
    Bar C: low=98.5 -- below the (now-active, from bar B's close) stop of 99
      -> the CORRECT implementation stops out HERE, at 99, reason "trail". A
      buggy implementation would never reach this bar (already exited at B).
    """
    bars = [
        (101.0, 99.5, 100.5),   # bar A
        (103.0, 98.0, 102.0),   # bar B -- crosses trigger, old-stop=97 survives, new-stop=99 set for C
        (102.2, 98.5, 99.0),    # bar C -- breaches the bar-B-set stop of 99
    ]
    walk = _walk_fixture(bars)
    assert walk["found_exit"] is True
    assert walk["trail_armed"] is True
    # THE critical assertion: the stop-out happens on bar C (the bar AFTER
    # the one that armed/ratcheted the trail), not on bar B itself.
    idx = pd.date_range("2024-03-04 13:15", periods=3, freq="15min")
    assert walk["exit_time"] == idx[2], (
        "look-ahead bug: the trail was checked against a stop level computed "
        "from the SAME bar's own high -- it must only ever use the stop level "
        "fixed by the END of the PRIOR bar"
    )
    assert walk["exit_price"] == pytest.approx(99.0)   # 103 (bar B high) - 4.0 (trail_dist)
    assert walk["exit_reason"] == "trail"
    assert walk["max_favorable_extreme"] == pytest.approx(103.0)


def test_chandelier_a_buggy_same_bar_implementation_would_disagree():
    """Sanity check on the fixture itself: prove the naive (WRONG) same-bar
    update-then-check ordering really would produce a different, earlier
    result than the engine's actual (correct) one -- otherwise the test above
    wouldn't actually be discriminating between the two behaviors."""
    entry_price, atr = 100.0, 2.0
    trigger, trail, stop_dist = 1.0 * atr, 2.0 * atr, 1.5 * atr
    bars = [(101.0, 99.5, 100.5), (103.0, 98.0, 102.0), (102.2, 98.5, 99.0)]

    # naive same-bar simulation
    stop = entry_price - stop_dist
    extreme = entry_price
    armed = False
    buggy_exit_bar = None
    for j, (h, l, c) in enumerate(bars):
        extreme = max(extreme, h)          # buggy: updates using bar j's OWN high first
        if not armed and (extreme - entry_price) >= trigger:
            armed = True
        if armed:
            stop = max(stop, extreme - trail)
        if l <= stop:                       # then checks bar j's OWN low against the just-updated stop
            buggy_exit_bar = j
            break
    assert buggy_exit_bar == 1, "fixture sanity check failed: expected the naive bug to fire on bar B (index 1)"

    walk = _walk_fixture(bars)
    correct_exit_bar = 2  # bar C, per the causal engine
    assert buggy_exit_bar != correct_exit_bar
    idx = pd.date_range("2024-03-04 13:15", periods=3, freq="15min")
    assert walk["exit_time"] == idx[correct_exit_bar]


def test_stop_never_loosens_after_arming():
    """Once armed, a pullback that makes a LOWER high than the running extreme
    must not move the stop back down (long) -- the trail only ratchets in the
    trade's favor."""
    entry_price, atr = 100.0, 2.0
    trigger, trail = 1.0 * atr, 2.0 * atr
    peak = entry_price + trigger + 1.0          # 103.0
    stop_after_a = peak - trail                  # 99.0
    bars = [
        (peak, entry_price - 0.1, peak - 0.5),                    # bar A: arms, ratchets to 99.0
        (entry_price + trigger + 0.2, stop_after_a + 0.2, entry_price + trigger + 0.1),  # bar B: lower high, must NOT loosen stop
        (entry_price + trigger + 0.2, stop_after_a + 0.1, entry_price + trigger + 0.1),  # bar C: still above stop
        (entry_price + trigger + 0.2, stop_after_a - 0.3, stop_after_a - 0.5),           # bar D: finally breaches
    ]
    walk = _walk_fixture(bars, entry_price=entry_price, atr_at_entry=atr)
    assert walk["found_exit"] is True
    idx = pd.date_range("2024-03-04 13:15", periods=4, freq="15min")
    assert walk["exit_time"] == idx[3]
    assert walk["exit_price"] == pytest.approx(stop_after_a)


def test_short_side_mirrors_the_long_side_ordering():
    """Same causal-ordering guarantee, mirrored for a short trade (extreme =
    running LOW, stop ratchets DOWN only)."""
    entry_price, atr = 100.0, 2.0
    trigger, trail = 1.0 * atr, 2.0 * atr
    bars = [
        (100.5, 99.0, 99.5),     # bar A: uneventful
        (102.0, 97.0, 98.0),     # bar B: crosses trigger (100-97=3>=2), would ratchet stop to 97+4=101;
                                  # bar B's own high (102) is BELOW the old stop (103) -> old stop survives
        (101.8, 97.5, 98.5),     # bar C: breaches the bar-B-set stop of 101
    ]
    walk = _walk_fixture(bars, entry_price=entry_price, atr_at_entry=atr, direction="short")
    assert walk["found_exit"] is True
    assert walk["trail_armed"] is True
    idx = pd.date_range("2024-03-04 13:15", periods=3, freq="15min")
    assert walk["exit_time"] == idx[2]
    assert walk["exit_price"] == pytest.approx(101.0)   # 97 (bar B low) + 4.0 (trail_dist)
    assert walk["exit_reason"] == "trail"


def test_never_armed_uses_initial_stop_only():
    bars = [(100.5, 99.2, 100.2), (100.6, 96.5, 97.0)]  # never reaches trigger (102); bar B breaches initial stop 97
    walk = _walk_fixture(bars)
    assert walk["found_exit"] is True
    assert walk["trail_armed"] is False
    assert walk["exit_reason"] == "stop"
    assert walk["exit_price"] == pytest.approx(97.0)  # 100 - 1.5*2.0
