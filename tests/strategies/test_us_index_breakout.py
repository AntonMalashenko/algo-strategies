"""Gate 0 coverage for strategies/us_index_breakout.py (S023, US Index Breakout).

All tests run on small hand-built M15 frames (flat "quiet" days plus a few
explicitly overridden bars), not on the real NSXUSD files, so they are fast
and fully deterministic. A flat quiet bar is open=close=100, high=100.5,
low=99.5 -> true range 1.0, so after the 14-bar warm-up ATR14 is exactly 1.0,
and a flat day's range window gives range_high=100.5 / range_low=99.5 ->
BUY STOP 102.5 / SELL STOP 97.5 with the base 2.0pt buffer.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from strategies.us_index_breakout import (US_INDEX_BREAKOUT_BASE, Trade, simulate,
                                          trades_to_frame)

NO_COST = US_INDEX_BREAKOUT_BASE.with_(cost_bps_roundtrip=0.0)
BARS_PER_DAY = 96  # full 24h of M15 bars


def _day(day: str, overrides: dict[str, tuple[float, float, float, float]] | None = None,
         drop: tuple[str, ...] = ()) -> pd.DataFrame:
    """One calendar day of flat M15 bars around 100, with optional per-bar
    (open, high, low, close) overrides keyed by "HH:MM" and dropped bars."""
    idx = pd.date_range(f"{day} 00:00", periods=BARS_PER_DAY, freq="15min")
    df = pd.DataFrame({"open": 100.0, "high": 100.5, "low": 99.5, "close": 100.0}, index=idx)
    for hhmm, (o, h, l, c) in (overrides or {}).items():
        df.loc[pd.Timestamp(f"{day} {hhmm}"), ["open", "high", "low", "close"]] = [o, h, l, c]
    if drop:
        df = df.drop(index=[pd.Timestamp(f"{day} {hhmm}") for hhmm in drop])
    return df


LONG_DAY = {"14:00": (102.0, 103.0, 101.9, 102.9),   # fills BUY STOP 102.5, stays above stop 101.5
            "14:15": (102.9, 105.0, 102.5, 104.8)}   # reaches TP 104.5
SHORT_DAY = {"14:00": (98.0, 98.1, 97.0, 97.1),      # fills SELL STOP 97.5, stays below stop 98.5
             "14:15": (97.1, 97.4, 95.0, 95.2)}      # reaches TP 95.5


def test_long_trade_geometry_stop_below_entry_tp_above():
    """Sanity: a long's stop sits below entry and its TP above, at exactly
    entry - stop_dist and entry + 2 * stop_dist, and the win books +2R.
    Guards against sign flips in the long branch of simulate()."""
    trades = simulate(_day("2024-01-02", LONG_DAY), NO_COST)
    assert len(trades) == 1
    t = trades[0]
    assert t.direction == "long"
    assert t.entry_price == 102.5
    assert t.stop_price < t.entry_price < t.tp_price
    assert t.stop_price == t.entry_price - t.stop_dist
    assert t.tp_price == t.entry_price + 2.0 * t.stop_dist
    assert t.exit_reason == "tp"
    assert t.r_multiple == 2.0


def test_short_trade_geometry_stop_above_entry_tp_below():
    """Mirror of the long test: a short's stop sits above entry, TP below."""
    trades = simulate(_day("2024-01-02", SHORT_DAY), NO_COST)
    assert len(trades) == 1
    t = trades[0]
    assert t.direction == "short"
    assert t.entry_price == 97.5
    assert t.tp_price < t.entry_price < t.stop_price
    assert t.stop_price == t.entry_price + t.stop_dist
    assert t.tp_price == t.entry_price - 2.0 * t.stop_dist
    assert t.exit_reason == "tp"
    assert t.r_multiple == 2.0


def test_atr_cap_engages_when_range_stop_is_wider_than_one_atr():
    """The documented SL interpretation: stop = min(entry -> opposite range
    boundary, 1.0 * ATR14). On the flat day the raw range-based distance is
    102.5 - 99.5 = 3.0 while ATR14 is 1.0, so the cap must bind (stop at
    101.5, not at range_low 99.5). With the cap loosened far beyond the raw
    distance the same trade must fall back to the opposite range boundary --
    proving the min() actually switches between the two."""
    t = simulate(_day("2024-01-02", LONG_DAY), NO_COST)[0]
    assert t.raw_stop_dist == 3.0
    assert t.atr_at_entry == 1.0
    assert t.atr_capped is True
    assert t.stop_dist == 1.0
    assert t.stop_price == 101.5

    loose = NO_COST.with_(atr_cap_mult=10.0)
    u = simulate(_day("2024-01-02", LONG_DAY), loose)[0]
    assert u.atr_capped is False
    assert u.stop_dist == 3.0
    assert u.stop_price == u.range_low == 99.5


def test_atr_is_taken_from_the_bar_before_the_entry_bar():
    """ATR at entry must NOT include the entry bar's own true range (unknown
    when the stop order fills mid-bar). The long entry bar here has TR 3.0;
    if it leaked in, ATR would move off exactly 1.0."""
    t = simulate(_day("2024-01-02", LONG_DAY), NO_COST)[0]
    assert t.atr_at_entry == 1.0


def test_bar_hitting_both_levels_skips_the_day():
    """A single bar spanning BUY STOP and SELL STOP is ambiguous (intra-bar
    order unknown): the day must be skipped outright -- not resolved to one
    side, and not re-armed for a later clean breakout the same day."""
    day = {"14:00": (100.0, 103.0, 97.0, 100.0),   # spans 102.5 and 97.5
           "15:00": (100.0, 103.0, 100.0, 102.9)}  # clean long breakout later -- must be ignored
    assert simulate(_day("2024-01-02", day), NO_COST) == []


def test_short_range_window_skips_the_day():
    """min_range_bars data-gap guard: with only 18 of 22 range-window bars
    present the range is not the spec's range, so no trade even though the
    entry window has a clean breakout."""
    day = _day("2024-01-02", LONG_DAY, drop=("09:00", "09:15", "09:30", "09:45"))
    assert simulate(day, NO_COST) == []


def test_no_breakout_means_no_trade_and_late_bars_are_ignored():
    """Levels are live only in [13:30, 18:00) UTC: a breakout bar at 18:00
    (outside the window) must not fire."""
    day = {"18:00": (102.0, 103.0, 101.9, 102.9)}
    assert simulate(_day("2024-01-02", day), NO_COST) == []


def test_unresolved_trade_is_force_closed_at_the_last_entry_window_bar():
    """Neither SL nor TP hit -> reason "time", exit at the 17:45 bar's close."""
    day = {"14:00": (102.0, 103.0, 101.9, 102.9)}
    for hhmm in pd.date_range("2024-01-02 14:15", "2024-01-02 17:45", freq="15min"):
        day[hhmm.strftime("%H:%M")] = (102.9, 103.0, 102.6, 102.8)
    t = simulate(_day("2024-01-02", day), NO_COST)[0]
    assert t.exit_reason == "time"
    assert t.exit_time == pd.Timestamp("2024-01-02 17:45")
    assert t.exit_price == 102.8


def _hand_built_days() -> pd.DataFrame:
    return pd.concat([
        _day("2024-01-02", LONG_DAY),
        _day("2024-01-03", SHORT_DAY),
        _day("2024-01-04", {"14:00": (100.0, 103.0, 97.0, 100.0)}),   # ambiguous -> skip
        _day("2024-01-05", {"13:45": (102.0, 102.6, 101.0, 101.2)}),  # long, then stopped same bar
        _day("2024-01-08", SHORT_DAY),
        _day("2024-01-09", LONG_DAY),
    ])


def _assert_prefix_identical(full: list[Trade], cut: list[Trade], cutoff: pd.Timestamp) -> None:
    before_full = [t for t in full if t.entry_time < cutoff]
    before_cut = [t for t in cut if t.entry_time < cutoff]
    assert len(before_full) > 0
    assert len(before_full) == len(before_cut)
    for a, b in zip(before_full, before_cut):
        assert a == b
        assert repr(a) == repr(b)


def test_no_lookahead_truncation_at_day_boundary_hand_built():
    """Gate 0: re-running on data truncated at a day boundary must reproduce
    every earlier trade byte-for-byte -- the range, ATR, entry scan and exit
    walk may not consult any bar after the one being evaluated."""
    bars = _hand_built_days()
    full = simulate(bars)
    assert len(full) == 5
    for cutoff in (pd.Timestamp("2024-01-04"), pd.Timestamp("2024-01-08"),
                   pd.Timestamp("2024-01-09")):
        cut = simulate(bars.loc[bars.index < cutoff])
        _assert_prefix_identical(full, cut, cutoff)


def _random_walk_bars(n_days: int, seed: int) -> pd.DataFrame:
    """Seeded random-walk M15 bars (deterministic), volatile enough to produce
    a mix of long/short/stop/tp/time outcomes."""
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2024-03-04", periods=n_days * BARS_PER_DAY, freq="15min")
    close = 15000.0 + np.cumsum(rng.normal(0.0, 8.0, len(idx)))
    open_ = np.r_[close[0], close[:-1]]
    wick = np.abs(rng.normal(0.0, 5.0, (2, len(idx))))
    high = np.maximum(open_, close) + wick[0]
    low = np.minimum(open_, close) - wick[1]
    return pd.DataFrame({"open": open_, "high": high, "low": low, "close": close}, index=idx)


def test_no_lookahead_truncation_random_walk_many_cutoffs():
    """Same Gate 0 proof on a 40-day seeded random walk and several cutoffs,
    including intraday ones: every trade that already EXITED before the
    cutoff must be identical, and every trade ENTERED before the cutoff must
    have identical entry-side fields (a still-open trade's exit legitimately
    differs -- the truncated run can only time-exit on the last bar it has)."""
    bars = _random_walk_bars(40, seed=23)
    full = simulate(bars)
    frame = trades_to_frame(full)
    assert len(full) >= 10
    assert set(frame["exit_reason"]) >= {"stop", "tp"}
    for cutoff in (pd.Timestamp("2024-03-15"), pd.Timestamp("2024-03-22 15:00"),
                   pd.Timestamp("2024-04-01 13:45"), pd.Timestamp("2024-04-10")):
        cut = simulate(bars.loc[bars.index < cutoff])
        cut_by_entry = {t.entry_time: t for t in cut}
        entered = [t for t in full if t.entry_time < cutoff]
        assert len(entered) == len(cut_by_entry)
        for t in entered:
            u = cut_by_entry[t.entry_time]
            for field in ("day", "direction", "range_high", "range_low", "entry_price",
                          "raw_stop_dist", "atr_at_entry", "stop_dist", "stop_price",
                          "tp_price"):
                assert getattr(t, field) == getattr(u, field), (cutoff, field)
            last_full_bar = t.exit_time + pd.Timedelta(minutes=15)
            if last_full_bar <= cutoff:
                assert t == u and repr(t) == repr(u)
