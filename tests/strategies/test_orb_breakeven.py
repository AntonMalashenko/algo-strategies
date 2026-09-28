"""Gate 0 / unit tests for S021's breakeven-stop modifier (OrbConfig.breakeven_at_r,
strategies/orb_intraday/engine.py's simulate()).

Covers only the NEW element this modifier adds -- the base ORB_BASE engine's own
byte-for-byte regression (n=1518, sum_net_pts=16220.614) is proven against the real
NSXUSD history in backtest/run_s021_breakeven.py's regression_check(), not re-proven
here (same scoping convention as tests/strategies/test_orb_squeeze_preset.py's own
docstring). What IS covered here, with small synthetic fixtures:

  1. breakeven_at_r=None (ORB_BASE's own value) never arms the modifier -- the exit
     loop is provably byte-identical to the pre-modifier stop-only path even on a
     fixture engineered to hit both the original stop AND what would be a breakeven
     trigger on the SAME bar.
  2. Long and short breakeven arming + exit-at-entry mechanics.
  3. The same-bar tie-break: when a single bar's range covers both the original stop
     AND the breakeven trigger, the ORIGINAL stop is assumed hit first (this
     project's established conservative same-bar convention, e.g.
     strategies/asia_sweep_reversion.py, strategies/gold_session_momentum.py) --
     breakeven never arms on that bar.
  4. Arming alone does not force an exit -- a trade can arm breakeven and still run
     to the ordinary end-of-session time exit at its real closing price.
  5. No look-ahead: appending a further, wildly-different trading day AFTER the day
     under test does not change that day's computed Trade in any field.
"""
from __future__ import annotations

from datetime import time

import pandas as pd

from strategies.orb_intraday.config import OrbConfig
from strategies.orb_intraday.engine import simulate

# adr_window=1 makes ADR14 for the test day exactly the single warm-up day's own
# session range (session_high - session_low), and makes compute_adr14's gap guard
# trivially satisfied (the lookback window is one day compared against itself, so
# span_days is always 0) -- this keeps fixtures to two calendar days instead of 14+.
# min_session_bars=1 lets each day supply as few bars as the scenario needs (only
# the 09:30 bar is structurally required, by _day_session's "sess.index[0] == lo"
# check); k_range/stop_adr_mult/session_open/session_close/entry_cutoff/
# cost_bps_roundtrip are left at ORB_BASE's real defaults.
BASE_CFG = OrbConfig(adr_window=1, min_session_bars=1, max_gap_days_per_session=100.0)

WARMUP_DATE = "2024-03-01"
TEST_DATE = "2024-03-04"
FUTURE_DATE = "2024-03-05"

# Warm-up day: one flat bar at session_open (09:30) with high=105/low=95 ->
# session_range = 10 -> ADR14 for the test day = 10 (adr_window=1).
WARMUP_BARS = {time(9, 30): (100.0, 105.0, 95.0, 100.0)}

# Test day open (the 09:30 bar's own open) = 100 -> with k_range=0.20 (default),
# U = 100 + 0.20*10 = 102, L = 100 - 0.20*10 = 98. With stop_adr_mult=0.75
# (default), stop_dist = 0.75*10 = 7.5 -- so a long entry at U=102 has
# stop_price=94.5, and a BE@1.0R trigger at entry+7.5 = 109.5.
FLAT_OPEN_BAR = {time(9, 30): (100.0, 100.0, 100.0, 100.0)}


def _m1_frame(day_bars: dict[str, dict[time, tuple[float, float, float, float]]]) -> pd.DataFrame:
    """Build an M1-indexed OHLC frame from {date_str: {time: (o, h, l, c)}}."""
    rows = []
    for date_str, bars in day_bars.items():
        base = pd.Timestamp(date_str)
        for t, (o, h, l, c) in bars.items():
            ts = base + pd.Timedelta(hours=t.hour, minutes=t.minute)
            rows.append({"dt": ts, "open": o, "high": h, "low": l, "close": c})
    df = pd.DataFrame(rows).set_index("dt").sort_index()
    return df[["open", "high", "low", "close"]]


def _trade_for(trades, date_str: str):
    d = pd.Timestamp(date_str)
    matches = [t for t in trades if t.day == d]
    assert len(matches) == 1, f"expected exactly one trade on {date_str}, got {len(matches)}"
    return matches[0]


# --- scenario bar sets for the test day (all long-side; short-side mirrors with L) -----
# 09:30 flat (no breakout yet); 09:31 breaks out long to U=102 (high=102, low stays
# above L=98); 09:32/09:33 vary per scenario below.
_ENTRY_BARS_LONG = {
    time(9, 30): (100.0, 100.0, 100.0, 100.0),
    time(9, 31): (100.0, 102.0, 99.0, 101.5),
}
_ENTRY_BARS_SHORT = {
    time(9, 30): (100.0, 100.0, 100.0, 100.0),
    time(9, 31): (100.0, 101.0, 98.0, 98.5),   # breaks out short to L=98
}


def test_breakeven_disabled_by_default_matches_stop_only_path():
    """breakeven_at_r=None (ORB_BASE's own value): even on a bar that touches BOTH the
    original stop (94.5) AND what would be a BE@1.0R trigger (109.5), the trade exits
    on the plain stop, exactly as if the modifier code did not exist at all."""
    cfg = BASE_CFG  # breakeven_at_r defaults to None
    day_bars = dict(_ENTRY_BARS_LONG)
    day_bars[time(9, 32)] = (100.0, 110.0, 90.0, 100.0)  # covers both 109.5 and 94.5
    day_bars[time(9, 33)] = (100.0, 100.0, 100.0, 100.0)

    m1 = _m1_frame({WARMUP_DATE: WARMUP_BARS, TEST_DATE: day_bars})
    trades = simulate(m1, cfg)
    tr = _trade_for(trades, TEST_DATE)

    assert tr.direction == "long"
    assert tr.entry_price == 102.0
    assert tr.stop_price == 94.5
    assert tr.exit_price == 94.5
    assert tr.exit_reason == "stop"
    assert tr.be_moved is False


def test_breakeven_long_arms_then_exits_at_entry_price():
    cfg = BASE_CFG.with_(breakeven_at_r=1.0)  # be_trigger = 102 + 1.0*7.5 = 109.5
    day_bars = dict(_ENTRY_BARS_LONG)
    day_bars[time(9, 32)] = (100.0, 110.0, 100.0, 105.0)  # arms BE (high >= 109.5), no stop hit
    day_bars[time(9, 33)] = (100.0, 103.0, 102.0, 102.0)  # low touches the moved stop (entry=102)

    m1 = _m1_frame({WARMUP_DATE: WARMUP_BARS, TEST_DATE: day_bars})
    trades = simulate(m1, cfg)
    tr = _trade_for(trades, TEST_DATE)

    assert tr.direction == "long"
    assert tr.entry_price == 102.0
    assert tr.stop_price == 94.5          # original stop distance recorded unchanged
    assert tr.be_moved is True
    assert tr.exit_reason == "breakeven"
    assert tr.exit_price == tr.entry_price == 102.0   # moved stop == entry, not the original 94.5
    assert tr.exit_time == pd.Timestamp(TEST_DATE) + pd.Timedelta(hours=9, minutes=33)


def test_breakeven_short_arms_then_exits_at_entry_price():
    cfg = BASE_CFG.with_(breakeven_at_r=1.0)  # entry=L=98, stop_dist=7.5 -> be_trigger=98-7.5=90.5
    day_bars = dict(_ENTRY_BARS_SHORT)
    day_bars[time(9, 32)] = (100.0, 100.0, 90.0, 95.0)   # arms BE (low <= 90.5), no stop hit
    day_bars[time(9, 33)] = (100.0, 98.0, 97.0, 98.0)    # high touches the moved stop (entry=98)

    m1 = _m1_frame({WARMUP_DATE: WARMUP_BARS, TEST_DATE: day_bars})
    trades = simulate(m1, cfg)
    tr = _trade_for(trades, TEST_DATE)

    assert tr.direction == "short"
    assert tr.entry_price == 98.0
    assert tr.be_moved is True
    assert tr.exit_reason == "breakeven"
    assert tr.exit_price == tr.entry_price == 98.0


def test_breakeven_same_bar_tie_break_prefers_original_stop():
    """A single bar whose range covers BOTH the original stop (94.5) and the BE@1.0R
    trigger (109.5) must exit on the ORIGINAL stop, never arm breakeven -- this
    project's conservative 'stop assumed hit first' same-bar convention."""
    cfg = BASE_CFG.with_(breakeven_at_r=1.0)
    day_bars = dict(_ENTRY_BARS_LONG)
    day_bars[time(9, 32)] = (100.0, 110.0, 90.0, 100.0)  # covers both 109.5 and 94.5 in one bar
    day_bars[time(9, 33)] = (100.0, 100.0, 100.0, 100.0)

    m1 = _m1_frame({WARMUP_DATE: WARMUP_BARS, TEST_DATE: day_bars})
    trades = simulate(m1, cfg)
    tr = _trade_for(trades, TEST_DATE)

    assert tr.be_moved is False
    assert tr.exit_reason == "stop"
    assert tr.exit_price == 94.5
    assert tr.exit_time == pd.Timestamp(TEST_DATE) + pd.Timedelta(hours=9, minutes=32)


def test_breakeven_armed_but_not_hit_runs_to_ordinary_time_exit():
    """Arming breakeven does not by itself force an exit: if price never comes back
    down to the moved stop, the trade still runs to the session's normal time exit,
    at the real last bar's close (not at entry, not at the original stop)."""
    cfg = BASE_CFG.with_(breakeven_at_r=1.0)
    day_bars = dict(_ENTRY_BARS_LONG)
    day_bars[time(9, 32)] = (100.0, 110.0, 100.0, 105.0)  # arms BE, no stop hit
    day_bars[time(9, 33)] = (105.0, 108.0, 105.0, 107.25)  # stays well above entry=102 -- never re-touches

    m1 = _m1_frame({WARMUP_DATE: WARMUP_BARS, TEST_DATE: day_bars})
    trades = simulate(m1, cfg)
    tr = _trade_for(trades, TEST_DATE)

    assert tr.be_moved is True
    assert tr.exit_reason == "time"
    assert tr.exit_price == 107.25  # the actual last bar's close, not entry/stop
    assert tr.exit_time == pd.Timestamp(TEST_DATE) + pd.Timedelta(hours=9, minutes=33)


def test_breakeven_no_look_ahead_across_days():
    """Appending a further trading day, with wild bars, strictly AFTER the day under
    test must not change anything about that day's already-computed Trade."""
    cfg = BASE_CFG.with_(breakeven_at_r=1.0)
    day_bars = dict(_ENTRY_BARS_LONG)
    day_bars[time(9, 32)] = (100.0, 110.0, 100.0, 105.0)
    day_bars[time(9, 33)] = (100.0, 103.0, 102.0, 102.0)

    m1_without_future = _m1_frame({WARMUP_DATE: WARMUP_BARS, TEST_DATE: day_bars})
    future_bars = {time(9, 30): (100.0, 100000.0, 0.01, 100.0)}  # deliberately absurd range
    m1_with_future = _m1_frame({WARMUP_DATE: WARMUP_BARS, TEST_DATE: day_bars, FUTURE_DATE: future_bars})

    tr_without = _trade_for(simulate(m1_without_future, cfg), TEST_DATE)
    tr_with = _trade_for(simulate(m1_with_future, cfg), TEST_DATE)

    assert tr_without == tr_with
