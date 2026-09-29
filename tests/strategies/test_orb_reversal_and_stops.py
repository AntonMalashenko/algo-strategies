"""Gate 0 / unit tests for S021 modifiers: reversal_mode and stop variants (trail_adr_mult, stop_on_close, time_stop_minutes, stop_adr_mult=inf) in engine.simulate().

The ORB_BASE byte-for-byte regression on real history (n=1518, sum_net_pts=16220.614) lives in
backtest/run_s021_reversal.py's regression_check(); these tests cover the new mechanics only,
on the same synthetic 2-day fixture shape as test_orb_breakeven.py (ADR=10 -> O=100, U=102,
L=98, stop_dist=7.5).
"""
from __future__ import annotations

from datetime import time

import pandas as pd
import pytest

from strategies.orb_intraday.config import (
    OrbConfig, REV_FADE, REV_SAR, REV_FLIP_OPPOSITE, REV_FLIP_OPEN,
)
from strategies.orb_intraday.engine import simulate

BASE_CFG = OrbConfig(adr_window=1, min_session_bars=1, max_gap_days_per_session=100.0)
WARMUP = {time(9, 30): (100.0, 105.0, 95.0, 100.0)}
OPEN = (100.0, 100.0, 100.0, 100.0)
BREAKOUT_LONG = (100.0, 102.0, 100.0, 101.0)


def _frame(test_bars):
    rows = []
    for date_str, bars in {"2024-03-01": WARMUP, "2024-03-04": test_bars}.items():
        base = pd.Timestamp(date_str)
        for t, (o, h, l, c) in bars.items():
            rows.append({"dt": base + pd.Timedelta(hours=t.hour, minutes=t.minute),
                         "open": o, "high": h, "low": l, "close": c})
    return pd.DataFrame(rows).set_index("dt").sort_index()[["open", "high", "low", "close"]]


def _run(cfg, bars):
    return [t for t in simulate(_frame(bars), cfg) if t.day == pd.Timestamp("2024-03-04")]


def test_fade_trades_against_breakout_at_touched_band():
    bars = {time(9, 30): OPEN, time(9, 31): BREAKOUT_LONG, time(15, 59): (104.0,) * 4}
    (t,) = _run(BASE_CFG.with_(reversal_mode=REV_FADE), bars)
    assert (t.direction, t.entry_price, t.stop_price) == ("short", 102.0, 109.5)
    assert (t.exit_reason, t.exit_price, t.gross_pts) == ("time", 104.0, -2.0)


def test_sar_opens_opposite_leg_at_stop_price():
    bars = {time(9, 30): OPEN, time(9, 31): BREAKOUT_LONG,
            time(9, 32): (100.0, 100.0, 94.0, 95.0), time(15, 59): (90.0,) * 4}
    t1, t2 = _run(BASE_CFG.with_(reversal_mode=REV_SAR), bars)
    assert (t1.leg, t1.exit_reason, t1.exit_price) == (1, "stop", 94.5)
    assert (t2.leg, t2.direction, t2.entry_price, t2.stop_price) == (2, "short", 94.5, 102.0)
    assert (t2.exit_reason, t2.exit_price, t2.gross_pts) == ("time", 90.0, 4.5)


def test_sar_respects_reversal_cutoff():
    bars = {time(9, 30): OPEN, time(9, 31): BREAKOUT_LONG,
            time(14, 45): (100.0, 100.0, 94.0, 95.0), time(15, 59): (90.0,) * 4}
    trades = _run(BASE_CFG.with_(reversal_mode=REV_SAR), bars)
    assert len(trades) == 1 and trades[0].exit_reason == "stop"


@pytest.mark.parametrize("mode,flip_price", [(REV_FLIP_OPPOSITE, 98.0), (REV_FLIP_OPEN, 100.0)])
def test_flip_closes_at_level_and_reverses(mode, flip_price):
    bars = {time(9, 30): OPEN, time(9, 31): BREAKOUT_LONG,
            time(9, 32): (101.0, 101.0, 97.5, 98.0), time(15, 59): (95.0,) * 4}
    t1, t2 = _run(BASE_CFG.with_(reversal_mode=mode), bars)
    assert (t1.exit_reason, t1.exit_price) == ("flip", flip_price)
    assert (t2.leg, t2.direction, t2.entry_price) == (2, "short", flip_price)
    assert t2.stop_price == flip_price + 7.5
    assert (t2.exit_reason, t2.exit_price) == ("time", 95.0)


def test_base_path_ignores_flip_fixture():
    bars = {time(9, 30): OPEN, time(9, 31): BREAKOUT_LONG,
            time(9, 32): (101.0, 101.0, 97.5, 98.0), time(15, 59): (95.0,) * 4}
    (t,) = _run(BASE_CFG, bars)
    assert (t.leg, t.exit_reason, t.exit_price) == (1, "time", 95.0)


def test_reversal_leg_stop_checked_on_flip_bar():
    # flip bar also spikes above the new short's stop (98 + 7.5 = 105.5) -> conservative stop
    bars = {time(9, 30): OPEN, time(9, 31): BREAKOUT_LONG,
            time(9, 32): (101.0, 106.0, 97.5, 98.0), time(15, 59): (95.0,) * 4}
    t1, t2 = _run(BASE_CFG.with_(reversal_mode=REV_FLIP_OPPOSITE), bars)
    assert (t2.exit_reason, t2.exit_price) == ("stop", 105.5)


def test_reversal_and_breakeven_not_combinable():
    with pytest.raises(ValueError):
        simulate(_frame({time(9, 30): OPEN}), BASE_CFG.with_(reversal_mode=REV_SAR, breakeven_at_r=1.0))


# --- stop-variant modifiers (trail_adr_mult / stop_on_close / time_stop_minutes) -------------

def test_trailing_stop_ratchets_and_applies_from_next_bar():
    # long @102, trail 0.5*ADR=5. 09:32 high 110 -> trail 105 (from next bar); 09:33 low 104.
    bars = {time(9, 30): OPEN, time(9, 31): BREAKOUT_LONG,
            time(9, 32): (103.0, 110.0, 103.0, 109.0), time(9, 33): (109.0, 109.0, 104.0, 104.5),
            time(15, 59): (120.0,) * 4}
    (t,) = _run(BASE_CFG.with_(trail_adr_mult=0.5), bars)
    assert (t.exit_reason, t.exit_price) == ("trail", 105.0)


def test_trailing_stop_same_bar_does_not_fire():
    # the bar that sets the new high also dips to the would-be trail level -> not stopped
    bars = {time(9, 30): OPEN, time(9, 31): BREAKOUT_LONG,
            time(9, 32): (103.0, 110.0, 104.0, 109.0), time(15, 59): (120.0,) * 4}
    (t,) = _run(BASE_CFG.with_(trail_adr_mult=0.5), bars)
    assert (t.exit_reason, t.exit_price) == ("time", 120.0)


def test_close_based_stop_ignores_wick_and_fills_at_close():
    # stop 94.5: 09:32 wick to 94 but closes 96 (no stop); 09:33 closes 93 -> exit 93
    bars = {time(9, 30): OPEN, time(9, 31): BREAKOUT_LONG,
            time(9, 32): (99.0, 99.0, 94.0, 96.0), time(9, 33): (96.0, 96.0, 92.0, 93.0),
            time(15, 59): (120.0,) * 4}
    (t,) = _run(BASE_CFG.with_(stop_on_close=True), bars)
    assert (t.exit_time.minute, t.exit_reason, t.exit_price) == (33, "stop", 93.0)


@pytest.mark.parametrize("close_at_check,expected", [(101.0, "time_stop"), (103.0, "time")])
def test_time_stop_exits_only_if_not_in_profit(close_at_check, expected):
    bars = {time(9, 30): OPEN, time(9, 31): BREAKOUT_LONG,
            time(10, 1): (close_at_check,) * 4, time(15, 59): (110.0,) * 4}
    (t,) = _run(BASE_CFG.with_(time_stop_minutes=30), bars)
    assert t.exit_reason == expected
    if expected == "time_stop":
        assert t.exit_price == close_at_check


def test_no_stop_is_time_exit_only():
    bars = {time(9, 30): OPEN, time(9, 31): BREAKOUT_LONG,
            time(9, 32): (99.0, 99.0, 50.0, 60.0), time(15, 59): (70.0,) * 4}
    (t,) = _run(BASE_CFG.with_(stop_adr_mult=float("inf")), bars)
    assert (t.exit_reason, t.exit_price) == ("time", 70.0)
