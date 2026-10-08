"""Unit tests for the S004-intraday prop rules (ALGODEV-62 phase A).

The two new engine flags live in strategies/fvg_mtf.py's run_backtest:
`intraday_cutoff` (rule 1) and `cost_inclusive_sizing` (rule 2). The frozen
base's own trade-for-trade regression against reports/s004_metalabel_dataset.csv
is proven on the real 2012-2026 history by
`python -m backtest.run_s004_intraday regression` (5018 trades, max|delta|
4.4e-16), not re-proven here. What IS covered here, on small synthetic
fixtures:

  1. Both flags at their S004_BASE values (None / False) leave the engine
     byte-identical to the pre-modifier path, even on a fixture that would be
     cut by the cutoff were it armed.
  2. A position still open on the cutoff bar is closed at that bar's close with
     exit_reason="intraday", and the round-trip cost is still charged.
  3. cost_inclusive_sizing makes a full stop lose exactly -1R (that is the
     entire point of the rule: it is what caps the planned worst day at -2R
     once the portfolio takes at most 2 trades a day).
  4. The portfolio daily cap keeps the first N entries of a server day across
     ALL pairs, not per pair.
"""
from __future__ import annotations

from datetime import time

import numpy as np
import pandas as pd
import pytest

from strategies.fvg_mtf import run_backtest
from strategies.s004_config import (
    MAX_ENTRY_SHIFT_PIPS,
    S004_BASE,
    S004_INTRADAY,
    S004_INTRADAY_NOSHIFT,
)

PIP = 10.0                  # raw price units per pip, as in the FX data files
SPREAD_PIPS = 0.9
COST = SPREAD_PIPS * PIP    # round-trip cost in raw price units
BAR = pd.Timedelta(minutes=15)
CUTOFF = time(22, 45)


def _m15(bars: list[tuple[str, float, float, float, float]]) -> pd.DataFrame:
    index = pd.to_datetime([t for t, *_ in bars])
    data = np.array([row[1:] for row in bars], dtype=float)
    return pd.DataFrame(data, index=index,
                        columns=["open", "high", "low", "close"]).assign(volume=1.0)


def _bullish_fvg_then_drift(day: str, drift_close: float,
                            touch_bar: tuple[float, float, float, float] = (112.0, 112.0, 109.0, 111.0)
                            ) -> pd.DataFrame:
    """One bullish H4 FVG, a touch that opens a long, then a flat drift to 23:00.

    The H4 bars are built by resampling, so the gap is created with three
    consecutive 4-hour blocks of M15 bars: block 0 high 100, block 2 low 110.
    The entry touch happens in block 3; afterwards price drifts without ever
    reaching the stop (zone bottom 100 - 2 pips) or the RR3 target.
    """
    bars: list[tuple[str, float, float, float, float]] = []

    def block(start_hour: int, low: float, high: float, close: float) -> None:
        start = pd.Timestamp(f"{day} {start_hour:02d}:00")
        for step in range(16):          # 16 M15 bars = one H4 bar
            stamp = (start + step * BAR).strftime("%Y-%m-%d %H:%M")
            bars.append((stamp, close, high, low, close))

    block(0, 95.0, 100.0, 98.0)         # H4 bar i   -- high 100
    block(4, 100.0, 115.0, 112.0)       # H4 bar i+1 -- the impulse
    block(8, 110.0, 118.0, 115.0)       # H4 bar i+2 -- low 110 > high 100 => gap [100, 110]
    # zone is tradable from 12:00; first touch of the zone top (110) opens a long
    touch = pd.Timestamp(f"{day} 12:00")
    bars.append((touch.strftime("%Y-%m-%d %H:%M"), *touch_bar))   # (open, high, low, close)
    stamp = touch + BAR
    end = pd.Timestamp(f"{day} 23:00")
    while stamp <= end:
        bars.append((stamp.strftime("%Y-%m-%d %H:%M"), drift_close, drift_close + 1.0,
                     drift_close - 1.0, drift_close))
        stamp += BAR
    return _m15(bars)


def test_base_values_leave_the_engine_unchanged():
    m15 = _bullish_fvg_then_drift("2024-03-04", drift_close=111.0)
    base = run_backtest(m15, pip=PIP, spread_pips=SPREAD_PIPS,
                        **{k: v for k, v in S004_BASE.engine_kwargs().items()
                           if k not in ("pip", "spread_pips")})
    legacy = run_backtest(m15, mode="base", stop="zone", rr=3.0,
                          pip=PIP, spread_pips=SPREAD_PIPS)
    pd.testing.assert_frame_equal(base, legacy)
    # the fixture does reach the cutoff bar, so "unchanged" is a real statement:
    # with the flag armed the same fixture produces an "intraday" exit instead.
    assert legacy.empty or "intraday" not in set(legacy["exit_reason"])


def test_open_position_is_closed_at_the_cutoff_bar_close():
    m15 = _bullish_fvg_then_drift("2024-03-04", drift_close=111.0)
    trades = run_backtest(m15, mode="base", stop="zone", rr=3.0,
                          pip=PIP, spread_pips=SPREAD_PIPS, intraday_cutoff=CUTOFF)
    assert len(trades) == 1
    trade = trades.iloc[0]
    assert trade["exit_reason"] == "intraday"
    assert trade["time_out"] == pd.Timestamp("2024-03-04 22:45")
    assert trade["exit"] == pytest.approx(111.0)
    expected_r = ((trade["exit"] - trade["entry"]) * trade["dir"] - COST) / \
                 abs(trade["entry"] - trade["sl"])
    assert trade["r"] == pytest.approx(expected_r)


def test_cost_inclusive_sizing_makes_a_full_stop_exactly_minus_one_r():
    # drift below the zone bottom: the stop (100 - 2 pips) is hit well before 22:45
    m15 = _bullish_fvg_then_drift("2024-03-04", drift_close=70.0)
    plain = run_backtest(m15, mode="base", stop="zone", rr=3.0,
                         pip=PIP, spread_pips=SPREAD_PIPS)
    sized = run_backtest(m15, mode="base", stop="zone", rr=3.0,
                         pip=PIP, spread_pips=SPREAD_PIPS, cost_inclusive_sizing=True)
    assert plain.iloc[0]["exit_reason"] == "sl"
    # same exit, same price -- only the R denominator changes
    assert sized.iloc[0]["exit"] == plain.iloc[0]["exit"]
    assert plain.iloc[0]["r"] < -1.0            # cost makes a raw-stop loss worse than -1R
    assert sized.iloc[0]["r"] == pytest.approx(-1.0)


def test_daily_cap_is_portfolio_wide_not_per_pair():
    from backtest.run_s004_intraday import apply_daily_cap

    day = pd.Timestamp("2024-03-04")
    # three different pairs entering on the same day, then a fourth entry;
    # a per-pair cap would take all four, the portfolio cap takes two.
    signals = pd.DataFrame({
        "symbol": ["EURUSD", "GBPUSD", "USDJPY", "EURUSD"],
        "time_in": [day + pd.Timedelta(hours=h) for h in (1, 2, 3, 4)],
    })
    capped = apply_daily_cap(signals, S004_INTRADAY.max_trades_per_day)
    assert list(capped["taken"]) == [True, True, False, False]
    # the next server day starts the count again
    next_day = signals.assign(time_in=signals["time_in"] + pd.Timedelta(days=1))
    both = apply_daily_cap(pd.concat([signals, next_day], ignore_index=True),
                           S004_INTRADAY.max_trades_per_day)
    assert both["taken"].sum() == 2 * S004_INTRADAY.max_trades_per_day
    assert apply_daily_cap(signals, None)["taken"].all()


def test_cutoff_exit_does_not_look_ahead():
    """Appending later bars must not change an already-cut trade in any field."""
    short = _bullish_fvg_then_drift("2024-03-04", drift_close=111.0)
    extra = _bullish_fvg_then_drift("2024-03-05", drift_close=300.0)
    long = pd.concat([short, extra])
    kwargs = dict(mode="base", stop="zone", rr=3.0, pip=PIP,
                  spread_pips=SPREAD_PIPS, intraday_cutoff=CUTOFF,
                  cost_inclusive_sizing=True)
    first_day_only = run_backtest(short, **kwargs)
    with_future = run_backtest(long, **kwargs)
    first = with_future[with_future["time_in"] < pd.Timestamp("2024-03-05")]
    pd.testing.assert_frame_equal(first_day_only, first.reset_index(drop=True))


def test_the_two_sizing_dials_are_bounded_by_the_daily_risk_budget():
    # The EA exposes both dials, so the pair has to be checkable here: rule 2
    # makes a full stop exactly -1R, which is what turns cap x risk into an
    # exact worst day rather than an estimate.
    assert S004_INTRADAY.worst_planned_day_pct() == pytest.approx(2.0)
    assert S004_BASE.worst_planned_day_pct() == float("inf")  # uncapped, backtest-only
    assert S004_INTRADAY.with_(max_trades_per_day=1, risk_pct=2.0).worst_planned_day_pct() == 2.0
    assert S004_INTRADAY.with_(max_trades_per_day=4, risk_pct=0.5).worst_planned_day_pct() == 2.0
    with pytest.raises(ValueError, match="worst day"):
        S004_INTRADAY.with_(risk_pct=1.5)        # 2 x 1.5% = -3%, over S004's share
    with pytest.raises(ValueError, match="worst day"):
        S004_INTRADAY.with_(max_trades_per_day=3)  # 3 x 1% = -3%, same breach


# --- entry_shift_pips: the limit sits `shift` pips from the edge, toward the bounce ---
# The fixture's zone is [100, 110] raw (one pip wide at PIP=10), so the long's near
# edge is 110 and a 0.5-pip shift puts the limit at 115.

SHIFT_PIPS = 0.5
SHIFT_RAW = SHIFT_PIPS * PIP
MIRROR = 300.0              # reflection axis for the short-side mirror test


def test_zero_shift_is_the_unshifted_engine():
    m15 = _bullish_fvg_then_drift("2024-03-04", drift_close=111.0)
    kwargs = {"mode": "base", "stop": "zone", "rr": 3.0, "pip": PIP, "spread_pips": SPREAD_PIPS,
              "intraday_cutoff": CUTOFF, "cost_inclusive_sizing": True}
    pd.testing.assert_frame_equal(run_backtest(m15, **kwargs),
                                  run_backtest(m15, entry_shift_pips=0.0, **kwargs))
    assert S004_INTRADAY_NOSHIFT.entry_shift_pips == 0.0
    assert S004_BASE.entry_shift_pips == 0.0


def test_shift_fills_a_bar_that_only_gets_near_the_edge():
    # open 120, low 114: never reaches the edge (110), but is inside the shifted limit (115).
    m15 = _bullish_fvg_then_drift("2024-03-04", drift_close=111.0,
                                  touch_bar=(120.0, 120.0, 114.0, 118.0))
    base = run_backtest(m15, mode="base", stop="zone", rr=3.0, pip=PIP,
                        spread_pips=SPREAD_PIPS, intraday_cutoff=CUTOFF)
    shifted = run_backtest(m15, mode="base", stop="zone", rr=3.0, pip=PIP,
                           spread_pips=SPREAD_PIPS, intraday_cutoff=CUTOFF,
                           entry_shift_pips=SHIFT_PIPS)
    touch = pd.Timestamp("2024-03-04 12:00")
    assert shifted.iloc[0]["time_in"] == touch
    assert shifted.iloc[0]["entry"] == pytest.approx(110.0 + SHIFT_RAW)   # the limit price, no better
    assert base.iloc[0]["time_in"] > touch                                # the base waited for 110


def test_shift_worsens_the_entry_but_not_the_stop_and_target_follow_the_new_risk():
    m15 = _bullish_fvg_then_drift("2024-03-04", drift_close=111.0)
    kw = {"mode": "base", "stop": "zone", "rr": 3.0, "pip": PIP, "spread_pips": SPREAD_PIPS,
          "intraday_cutoff": CUTOFF}
    base = run_backtest(m15, **kw).iloc[0]
    shifted = run_backtest(m15, entry_shift_pips=SHIFT_PIPS, **kw).iloc[0]
    # the bar opens at 112, inside the shifted limit (115): it fills at the open, which is
    # worse than the base's 110 and better than the limit's 115
    assert shifted["entry"] == pytest.approx(112.0)
    assert shifted["entry"] > base["entry"]
    assert shifted["sl"] == pytest.approx(base["sl"])                    # stop stays behind the far edge
    risk = shifted["entry"] - shifted["sl"]
    assert shifted["tp"] == pytest.approx(shifted["entry"] + 3.0 * risk)


def test_shift_is_symmetric_for_shorts():
    # mirror image of the long fixture (price -> 300 - price, kept positive): a bearish FVG, a short.
    m15 = _bullish_fvg_then_drift("2024-03-04", drift_close=111.0,
                                  touch_bar=(120.0, 120.0, 114.0, 118.0))
    mirrored = m15.copy()
    mirrored["open"], mirrored["close"] = MIRROR - m15["open"], MIRROR - m15["close"]
    mirrored["high"], mirrored["low"] = MIRROR - m15["low"], MIRROR - m15["high"]
    long = run_backtest(m15, mode="base", stop="zone", rr=3.0, pip=PIP,
                        spread_pips=SPREAD_PIPS, intraday_cutoff=CUTOFF,
                        entry_shift_pips=SHIFT_PIPS).iloc[0]
    short = run_backtest(mirrored, mode="base", stop="zone", rr=3.0, pip=PIP,
                         spread_pips=SPREAD_PIPS, intraday_cutoff=CUTOFF,
                         entry_shift_pips=SHIFT_PIPS).iloc[0]
    assert short["dir"] == -long["dir"]
    assert short["time_in"] == long["time_in"]
    assert short["entry"] == pytest.approx(MIRROR - long["entry"])


def test_config_rejects_a_shift_outside_the_tested_range():
    with pytest.raises(ValueError, match="tested range"):
        S004_INTRADAY.with_(entry_shift_pips=MAX_ENTRY_SHIFT_PIPS + 0.1)
    with pytest.raises(ValueError, match="tested range"):
        S004_INTRADAY.with_(entry_shift_pips=-0.1)
    assert S004_INTRADAY.engine_kwargs()["entry_shift_pips"] == S004_INTRADAY.entry_shift_pips
