"""Gate 0 / unit tests for S021.1 -- NAS100 Opening-Range Squeeze
(strategies/orb_intraday/engine.py's simulate_squeeze_preset / compute_orb_open_range /
compute_squeeze_filters / compute_daily_bars_utc; config.py's NAS_SQUEEZE_PRESET).

Covers only the NEW elements this preset adds, per the spec's own scoping
(claude/prompts-new-strategy-candidates.md P14, "S021.1" hard-constraints section: "for
S021.1, only new-element tests needed -- the base engine is already Gate-0-proven"). The
base ORB_BASE/simulate() engine's own byte-for-byte regression (n=1518,
sum_net_pts=16220.614) is checked in backtest/run_s021_squeeze_preset.py, not re-proven
here -- see that runner's own header comment.
"""
from __future__ import annotations

from datetime import time

import pandas as pd
import pytest

from strategies.orb_intraday.config import NAS_SQUEEZE_PRESET, OrbConfig
from strategies.orb_intraday.engine import (
    compute_daily_bars_utc,
    compute_orb_open_range,
    compute_squeeze_filters,
    ema,
    simulate_squeeze_preset,
    squeeze_trades_to_frame,
)
from utils.indicators import wilder_atr

# Short warm-up periods so fixtures stay small -- NAS_SQUEEZE_PRESET's real periods (14/50)
# are exercised in backtest/run_s021_squeeze_preset.py against real data, not here.
CFG = NAS_SQUEEZE_PRESET.with_(squeeze_atr_period=2, trend_ema_period=2)


def _m15_frame(day_slots: dict[str, dict[time, tuple[float, float, float, float]]]) -> pd.DataFrame:
    """Build a sparse 'M1 UTC' frame with exactly one row per given 15-minute-aligned slot.
    Every timestamp here is already on a 15-minute boundary, so resample_ohlc('15min')
    (called inside simulate_squeeze_preset/compute_orb_open_range's caller) reduces each
    single row to its own unchanged M15 bar -- first/max/min/last of one row is that row --
    letting these tests build compact fixtures directly at M15-slot granularity, through the
    SAME resample_ohlc code path the engine itself uses, instead of full 1-minute bars."""
    rows = []
    for date_str, slots in day_slots.items():
        base = pd.Timestamp(date_str)
        for t, (o, h, l, c) in slots.items():
            ts = base + pd.Timedelta(hours=t.hour, minutes=t.minute)
            rows.append({"dt": ts, "open": o, "high": h, "low": l, "close": c})
    df = pd.DataFrame(rows).set_index("dt").sort_index()
    return df[["open", "high", "low", "close"]]


# --- shared warm-up blocks -------------------------------------------------------------
# Four single-bar "days" (one flat daily OHLC bar at an arbitrary time -- only used to seed
# the daily ATR14/EMA2 history the squeeze/trend filters read; these days have no
# orb-window bar at all, so they can never themselves produce a trade). With
# squeeze_atr_period=2 and trend_ema_period=2, both filters first become defined (after the
# compute_squeeze_filters one-day shift) on the 5th calendar day -- exactly the test day
# appended after these four in each scenario below.
_UP_WARMUP = {
    "2024-01-02": {time(12, 0): (7000, 7010, 6990, 7000)},
    "2024-01-03": {time(12, 0): (7000, 7015, 6995, 7005)},
    "2024-01-04": {time(12, 0): (7005, 7020, 6995, 7010)},
    "2024-01-05": {time(12, 0): (7010, 7030, 7000, 7020)},
}
_DOWN_WARMUP = {
    "2024-01-02": {time(12, 0): (7000, 7010, 6990, 7000)},
    "2024-01-03": {time(12, 0): (7000, 7005, 6985, 6995)},
    "2024-01-04": {time(12, 0): (6995, 7005, 6980, 6990)},
    "2024-01-05": {time(12, 0): (6990, 7000, 6970, 6980)},
}
TEST_DAY = "2024-01-08"  # 5th calendar day in the fixture -- first day filters are defined


def _entry_window_slots(touch_price: float, direction: str, touch_at=time(14, 15),
                         hi_base=7045.0, lo_base=7042.0):
    """9 quarter-hour slots 13:45..15:45 (the entry window). Every slot is flat and stays
    clear of touch_price except touch_at, whose high (long) / low (short) reaches exactly
    touch_price -- the single bar the resting stop order should trigger on."""
    slots = {}
    t = time(13, 45)
    times = [time(13, 45), time(14, 0), time(14, 15), time(14, 30), time(14, 45),
             time(15, 0), time(15, 15), time(15, 30), time(15, 45)]
    for tt in times:
        if tt == touch_at:
            if direction == "long":
                slots[tt] = (hi_base, touch_price, lo_base, hi_base)
            else:
                slots[tt] = (lo_base, hi_base, touch_price, lo_base)
        else:
            slots[tt] = (hi_base, hi_base, lo_base, hi_base) if direction == "long" \
                else (lo_base, hi_base, lo_base, lo_base)
    return slots


def _flat_slot(px):
    return (px, px, px, px)


def test_orb_open_range_uses_only_its_own_window_and_is_causal():
    day1 = {
        time(13, 15): _flat_slot(100.0),   # before orb_open_start -- must be excluded
        time(13, 30): (100.0, 105.0, 98.0, 102.0),  # the ONE candle in [13:30, 13:45)
        time(13, 45): _flat_slot(999.0),   # at orb_open_end -- exclusive, must be excluded
    }
    day2 = {time(13, 30): (200.0, 210.0, 195.0, 205.0)}
    frame_2days = _m15_frame({"2024-02-01": day1, "2024-02-02": day2})
    frame_1day = _m15_frame({"2024-02-01": day1})

    m15_2 = frame_2days  # already at 15-min slots -- resample happens inside the callee
    out_2 = compute_orb_open_range(m15_2, CFG)
    out_1 = compute_orb_open_range(frame_1day, CFG)

    d1 = pd.Timestamp("2024-02-01")
    assert out_2.at[d1, "orb_high"] == 105.0
    assert out_2.at[d1, "orb_low"] == 98.0
    # day1's row is identical whether or not day2's (later) data exists -- no look-ahead.
    assert out_1.at[d1, "orb_high"] == out_2.at[d1, "orb_high"]
    assert out_1.at[d1, "orb_low"] == out_2.at[d1, "orb_low"]


def test_squeeze_filters_are_causal_one_day_shift():
    dates = pd.to_datetime(["2024-01-01", "2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05"])
    daily = pd.DataFrame({
        "open":  [10.0, 10.0, 10.5, 11.0, 11.5],
        "high":  [10.5, 10.8, 11.2, 11.6, 12.0],
        "low":   [9.5, 9.7, 10.1, 10.6, 11.1],
        "close": [10.0, 10.5, 11.0, 11.4, 11.8],
    }, index=dates)

    oracle_atr = wilder_atr(daily["high"], daily["low"], daily["close"], period=CFG.squeeze_atr_period)
    oracle_ema = ema(daily["close"], CFG.trend_ema_period)

    filt_full = compute_squeeze_filters(daily, CFG)
    for i in range(1, len(dates)):
        d = dates[i]
        exp_atr, exp_ema, exp_close = oracle_atr.iloc[i - 1], oracle_ema.iloc[i - 1], daily["close"].iloc[i - 1]
        got_atr, got_ema, got_close = filt_full.at[d, "prior_atr14"], filt_full.at[d, "prior_ema"], filt_full.at[d, "prior_close"]
        if pd.isna(exp_atr):
            assert pd.isna(got_atr)
        else:
            assert got_atr == pytest.approx(exp_atr)
        if pd.isna(exp_ema):
            assert pd.isna(got_ema)
        else:
            assert got_ema == pytest.approx(exp_ema)
        assert got_close == pytest.approx(exp_close)

    # day0 has nothing strictly before it -- must be entirely NaN.
    assert pd.isna(filt_full.at[dates[0], "prior_atr14"])
    assert pd.isna(filt_full.at[dates[0], "prior_ema"])
    assert pd.isna(filt_full.at[dates[0], "prior_close"])

    # No look-ahead: dropping the LAST day must not change any earlier day's row.
    filt_short = compute_squeeze_filters(daily.iloc[:-1], CFG)
    for i in range(len(dates) - 1):
        d = dates[i]
        for col in ("prior_atr14", "prior_ema", "prior_close"):
            a, b = filt_full.at[d, col], filt_short.at[d, col]
            if pd.isna(a):
                assert pd.isna(b)
            else:
                assert a == pytest.approx(b)


def _build_test_day(warmup, orb_hi, orb_lo, direction, entry_touch_at=time(14, 15),
                     post_entry_slots=None):
    day = dict(warmup)
    slots = {time(13, 30): (orb_lo, orb_hi, orb_lo, orb_hi)}
    entry_price = (orb_hi + CFG.orb_entry_buffer_pts) if direction == "long" \
        else (orb_lo - CFG.orb_entry_buffer_pts)
    slots.update(_entry_window_slots(entry_price, direction, touch_at=entry_touch_at,
                                      hi_base=orb_hi - 1, lo_base=orb_lo + 1))
    if post_entry_slots:
        slots.update(post_entry_slots)
    day[TEST_DAY] = slots
    return _m15_frame(day)


def test_trend_and_squeeze_filters_pick_long_on_uptrend_warmup():
    # squeeze_atr_mult=0.7 baseline; prior_atr14 here is 26.25 (hand-verified against the
    # same warmup in test_squeeze_filters_are_causal_one_day_shift's oracle machinery) --
    # a narrow 10pt ORB (< 0.7*26.25=18.375) must pass the squeeze filter.
    frame = _build_test_day(_UP_WARMUP, orb_hi=7050.0, orb_lo=7040.0, direction="long")
    trades = squeeze_trades_to_frame(simulate_squeeze_preset(frame, CFG))
    assert len(trades) == 1
    assert trades.iloc[0]["direction"] == "long"


def test_trend_and_squeeze_filters_pick_short_on_downtrend_warmup():
    frame = _build_test_day(_DOWN_WARMUP, orb_hi=6960.0, orb_lo=6950.0, direction="short")
    trades = squeeze_trades_to_frame(simulate_squeeze_preset(frame, CFG))
    assert len(trades) == 1
    assert trades.iloc[0]["direction"] == "short"


def test_squeeze_filter_blocks_a_wide_opening_range():
    # Same warmup (prior_atr14=26.25) but a 30pt ORB (>= 0.7*26.25=18.375) -- must be
    # skipped entirely: zero trades, not a degraded/forced one.
    frame = _build_test_day(_UP_WARMUP, orb_hi=7070.0, orb_lo=7040.0, direction="long")
    trades = squeeze_trades_to_frame(simulate_squeeze_preset(frame, CFG))
    assert len(trades) == 0


def test_stop_order_never_triggers_without_a_touch():
    day = dict(_UP_WARMUP)
    orb_hi, orb_lo = 7050.0, 7040.0
    slots = {time(13, 30): (orb_lo, orb_hi, orb_lo, orb_hi)}
    # entry level = 7052.0; every entry-window bar stays strictly below it.
    for t in (time(13, 45), time(14, 0), time(14, 15), time(14, 30)):
        slots[t] = (7045.0, 7049.0, 7043.0, 7045.0)
    day[TEST_DAY] = slots
    frame = _m15_frame(day)
    trades = squeeze_trades_to_frame(simulate_squeeze_preset(frame, CFG))
    assert len(trades) == 0


def test_entry_fires_on_first_touch_not_a_later_one():
    # Two bars both cross the entry level -- the FIRST one (14:00) must be the fill, and
    # exactly one trade must result even though a later bar also touches it.
    orb_hi, orb_lo = 7050.0, 7040.0
    entry_price = orb_hi + CFG.orb_entry_buffer_pts
    day = dict(_UP_WARMUP)
    slots = {time(13, 30): (orb_lo, orb_hi, orb_lo, orb_hi),
             time(13, 45): (7045.0, 7049.0, 7043.0, 7045.0),
             time(14, 0):  (7050.0, entry_price + 1.0, 7048.0, 7050.0),   # first touch
             time(14, 15): (7050.0, entry_price + 5.0, 7048.0, 7050.0),  # would ALSO touch
             time(14, 30): (7050.0, 7054.0, 7048.0, 7050.0),
             # rest of the day, flat, forces a "time" exit so the trade resolves cleanly
             time(14, 45): (7050.0, 7054.0, 7048.0, 7050.0)}
    for h in range(15, 20):
        slots[time(h, 0)] = (7050.0, 7054.0, 7048.0, 7050.0)
        slots[time(h, 15)] = (7050.0, 7054.0, 7048.0, 7050.0)
        slots[time(h, 30)] = (7050.0, 7054.0, 7048.0, 7050.0)
        slots[time(h, 45)] = (7050.0, 7054.0, 7048.0, 7050.0)
    day[TEST_DAY] = slots
    frame = _m15_frame(day)
    trades = squeeze_trades_to_frame(simulate_squeeze_preset(frame, CFG))
    assert len(trades) == 1
    assert trades.iloc[0]["entry_time"] == pd.Timestamp(TEST_DAY) + pd.Timedelta(hours=14)
    assert trades.iloc[0]["entry_price"] == pytest.approx(entry_price)


def test_stop_is_opposite_orb_boundary_and_tp_is_r_multiple():
    orb_hi, orb_lo = 7050.0, 7040.0
    entry_price = orb_hi + CFG.orb_entry_buffer_pts
    stop_dist = entry_price - orb_lo
    tp_expected = entry_price + CFG.orb_tp_r_mult * stop_dist
    post = {}
    for h in range(15, 20):
        for m in (0, 15, 30, 45):
            post[time(h, m)] = (7050.0, 7054.0, 7048.0, 7050.0)
    frame = _build_test_day(_UP_WARMUP, orb_hi, orb_lo, "long", post_entry_slots=post)
    trades = squeeze_trades_to_frame(simulate_squeeze_preset(frame, CFG))
    assert len(trades) == 1
    tr = trades.iloc[0]
    assert tr["stop_price"] == pytest.approx(orb_lo)
    assert tr["stop_dist"] == pytest.approx(stop_dist)
    assert tr["tp_price"] == pytest.approx(tp_expected)


def test_same_bar_stop_and_tp_resolves_as_stop():
    orb_hi, orb_lo = 7050.0, 7040.0
    entry_price = orb_hi + CFG.orb_entry_buffer_pts  # 7052.0
    stop_price = orb_lo                              # 7040.0
    tp_price = entry_price + CFG.orb_tp_r_mult * (entry_price - stop_price)
    post = {time(15, 0): (7050.0, tp_price + 5.0, stop_price - 5.0, 7050.0)}  # both hit
    for h in range(15, 20):
        for m in (0, 15, 30, 45):
            post.setdefault(time(h, m), (7050.0, 7054.0, 7048.0, 7050.0))
    frame = _build_test_day(_UP_WARMUP, orb_hi, orb_lo, "long", post_entry_slots=post)
    trades = squeeze_trades_to_frame(simulate_squeeze_preset(frame, CFG))
    assert len(trades) == 1
    tr = trades.iloc[0]
    assert tr["exit_reason"] == "stop"
    assert tr["exit_price"] == pytest.approx(stop_price)


def test_tp_hit_resolves_as_tp_when_stop_not_touched():
    orb_hi, orb_lo = 7050.0, 7040.0
    entry_price = orb_hi + CFG.orb_entry_buffer_pts
    tp_price = entry_price + CFG.orb_tp_r_mult * (entry_price - orb_lo)
    post = {time(15, 0): (7050.0, tp_price + 3.0, 7048.0, 7050.0)}  # TP only, stop untouched
    for h in range(15, 20):
        for m in (0, 15, 30, 45):
            post.setdefault(time(h, m), (7050.0, 7054.0, 7048.0, 7050.0))
    frame = _build_test_day(_UP_WARMUP, orb_hi, orb_lo, "long", post_entry_slots=post)
    trades = squeeze_trades_to_frame(simulate_squeeze_preset(frame, CFG))
    assert len(trades) == 1
    tr = trades.iloc[0]
    assert tr["exit_reason"] == "tp"
    assert tr["exit_price"] == pytest.approx(tp_price)


def test_forced_time_exit_when_neither_stop_nor_tp_hit():
    orb_hi, orb_lo = 7050.0, 7040.0
    post = {}
    for h in range(15, 20):
        for m in (0, 15, 30, 45):
            post[time(h, m)] = (7050.0, 7054.0, 7048.0, 7049.5)  # stays inside SL/TP the whole way
    frame = _build_test_day(_UP_WARMUP, orb_hi, orb_lo, "long", post_entry_slots=post)
    trades = squeeze_trades_to_frame(simulate_squeeze_preset(frame, CFG))
    assert len(trades) == 1
    tr = trades.iloc[0]
    assert tr["exit_reason"] == "time"
    assert tr["exit_time"] == pd.Timestamp(TEST_DAY) + pd.Timedelta(hours=19, minutes=45)
    assert tr["exit_price"] == pytest.approx(7049.5)


def test_empty_input_returns_no_trades():
    empty = pd.DataFrame(columns=["open", "high", "low", "close"])
    empty.index = pd.DatetimeIndex([])
    assert simulate_squeeze_preset(empty, CFG) == []
    assert compute_orb_open_range(empty, CFG).empty
    assert compute_daily_bars_utc(empty).empty


def test_gate0_no_look_ahead_truncation():
    orb_hi, orb_lo = 7050.0, 7040.0
    post = {time(15, 0): (7050.0, 7054.0, 7048.0, 7050.0),
            time(15, 15): (7050.0, 7083.0, 7048.0, 7050.0)}  # TP (7082) hit here
    for h in range(15, 20):
        for m in (0, 15, 30, 45):
            post.setdefault(time(h, m), (7050.0, 7054.0, 7048.0, 7050.0))
    full = _build_test_day(_UP_WARMUP, orb_hi, orb_lo, "long", post_entry_slots=post)
    # append a later, independent day so there IS future data available to leak from.
    later = {time(12, 0): (9000.0, 9010.0, 8990.0, 9000.0)}
    full = pd.concat([full, _m15_frame({"2024-01-09": later})]).sort_index()

    trades_full = squeeze_trades_to_frame(simulate_squeeze_preset(full, CFG))
    assert len(trades_full) == 1
    exit_time = trades_full.iloc[0]["exit_time"]

    cutoff = exit_time  # truncate right at the resolving bar -- nothing later is visible
    truncated = full.loc[full.index <= cutoff]
    trades_trunc = squeeze_trades_to_frame(simulate_squeeze_preset(truncated, CFG))

    pd.testing.assert_frame_equal(trades_trunc.reset_index(drop=True),
                                   trades_full.reset_index(drop=True))
