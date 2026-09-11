"""Regression coverage for strategies/ger40_lonfra/engine.py's per-position
`tp` field -- see engine.py::_simulate_leg's docstring and the "each position
carries its own tp" comment in bot/s007_signals.py.

Found live 2026-08-13: a commit dropped `tp=tp` from both position-dict
constructors in `_simulate_leg` (present before, silently removed). Harmless
for the PRIMARY leg (its own `tp` param equals what s007_signals.py's
`p.get("tp", tp)` fallback would use anyway), but it broke the
`b_reversal_to_A` recovery leg: that leg is simulated with `tp_A` (the
OPPOSITE-direction target), and without its own stored `p["tp"]`, the
fallback silently substituted the PRIMARY leg's tp instead -- a target on the
wrong side of entry. Live effect: cTrader rejected every recovery-leg order
with `TRADING_BAD_STOPS: New TP for SELL pending order should be < entry
price`, for ~27 minutes straight (10:19-10:44) during S007's live session,
until the setups aged out and resolved as stop-loss ghosts without ever
reaching the broker.

tests/bot/test_s007_signals.py's `test_a_wanted_position_uses_its_own_tp_...`
covers the CONSUMER side (s007_signals.py correctly prefers p["tp"] when
present) but mocks simulate_day() entirely, so it never exercised the real
engine code that had the regression. These tests call the real
`_simulate_leg`/`simulate_day` instead, so a future regression here fails
loudly again.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from strategies.ger40_lonfra.config import StrategyConfig
from strategies.ger40_lonfra.engine import (BE_OFFSET_MIN_GAP_POINTS, _simulate_leg,
                                            simulate_day)
from strategies.ger40_lonfra.structure import structure_levels


def _long_leg(cfg, entry=100.0, stop=90.0, tp=200.0):
    """One long position, risk0 = 10, whose price path crosses the BE@0.5R
    trigger (105) on bar 1 and then retraces onto whatever the moved stop is.
    Returns the single position dict."""
    highs = np.array([101.0, 106.0, 104.0, 102.0, 101.0, 100.0])
    lows = np.array([99.5, 99.5, 103.0, 102.0, 100.0, 99.5])
    closes = np.array([100.0, 105.0, 103.5, 102.0, 100.5, 99.5])
    L = structure_levels(highs, lows, cfg.k)
    positions, _ = _simulate_leg(highs, lows, closes, L, 0, entry, True,
                                 tp, stop, cfg, buffer=0)
    return positions[0]


def test_simulate_leg_stores_its_own_tp_on_primary_and_add_positions():
    n = 20
    highs = np.full(n, 100.0)
    lows = np.full(n, 99.0)
    closes = np.full(n, 99.5)
    cfg = StrategyConfig(stop_mode="mid_range", do_pyramid=False)
    L = structure_levels(highs, lows, cfg.k)

    up_positions, _ = _simulate_leg(highs, lows, closes, L, 0, 99.5, True,
                                    105.0, 99.0, cfg, buffer=0)
    assert up_positions[0]["tp"] == 105.0

    # A DIFFERENT tp than the up leg's -- the exact shape of the recovery
    # leg's call (opposite direction, opposite/own target).
    down_positions, _ = _simulate_leg(highs, lows, closes, L, 0, 99.5, False,
                                      90.0, 100.0, cfg, buffer=0)
    assert down_positions[0]["tp"] == 90.0


def test_stop0_survives_breakeven_move_unlike_stop():
    """ALGODEV-40: p["stop0"] is the position's real stop as of creation,
    stored once and never mutated afterward -- unlike p["stop"], which the
    breakeven block collapses to p["entry"] once armed. bot/s007_signals.py
    reads stop0 (not a risk0-based re-derivation) to get the correct
    pre-breakeven stop for a fresh live placement."""
    n = 20
    highs = np.full(n, 100.0)   # risk0=0.5 (entry 99.5, stop 99.0); trig=99.75 <= 100 -> fires
    lows = np.full(n, 99.2)     # stays above the 99.0 stop -- no premature same-bar stop-out
    closes = np.full(n, 99.5)
    cfg = StrategyConfig(stop_mode="mid_range", do_pyramid=False, breakeven_at_r=0.5)
    L = structure_levels(highs, lows, cfg.k)

    positions, _ = _simulate_leg(highs, lows, closes, L, 0, 99.5, True,
                                 105.0, 99.0, cfg, buffer=0)
    p = positions[0]
    assert p["be_moved"] is True
    assert p["stop"] == p["entry"] == 99.5      # collapsed by breakeven
    assert p["stop0"] == 99.0                   # real creation-time stop, unmutated


def test_breakeven_offset_points_defaults_to_the_entry_exact_move():
    """ALGODEV-41: breakeven_offset_points=0.0 (the default on every preset
    but an explicit _OFF<N> one) must reproduce the pre-offset behaviour
    exactly -- stop collapses to entry, and the resulting BE exit is a gross
    0.0R that only turns into a small loss once costs are applied. This is
    what the REF_* regression relies on."""
    cfg = StrategyConfig(stop_mode="mid_range", do_pyramid=False, breakeven_at_r=0.5)
    p = _long_leg(cfg)
    assert p["be_moved"] is True
    assert p["stop"] == p["entry"] == 100.0
    assert p["status"] == "stop" and p["exit"] == 100.0


def test_breakeven_offset_points_moves_the_stop_into_profit():
    """The offset puts the BE stop `offset` points PAST entry in the profit
    direction, so the exit books a small WIN instead of the round-trip
    spread. stop0 (the pre-breakeven stop) is untouched, as always."""
    cfg = StrategyConfig(stop_mode="mid_range", do_pyramid=False, breakeven_at_r=0.5,
                         breakeven_offset_points=3.0)
    p = _long_leg(cfg)
    assert p["be_moved"] is True
    assert p["be_offset"] == 3.0
    assert p["stop"] == 103.0                      # entry 100 + 3pt
    assert p["status"] == "stop" and p["exit"] == 103.0
    assert p["stop0"] == 90.0                      # creation-time stop, unmutated
    assert p["risk0"] == 10.0                      # R still normalized on creation risk


def test_breakeven_offset_points_clamped_below_the_trigger_price():
    """An offset >= breakeven_at_r * risk0 would place the "moved" stop at or
    beyond the very price that armed it -- an instant stop-out that is a
    same-bar artifact, not a real exit. The engine clamps the offset to
    BE_OFFSET_MIN_GAP_POINTS below the trigger."""
    cfg = StrategyConfig(stop_mode="mid_range", do_pyramid=False, breakeven_at_r=0.5,
                         breakeven_offset_points=99.0)
    p = _long_leg(cfg)                              # risk0 10 -> trigger at 105
    assert p["be_offset"] == 5.0 - BE_OFFSET_MIN_GAP_POINTS
    assert p["stop"] == 104.0 < 105.0


def test_breakeven_offset_points_mirrors_for_a_short():
    """Short side: the stop goes entry - offset, i.e. also into profit."""
    highs = np.array([100.5, 100.0, 96.0, 97.5, 98.0, 99.0])
    lows = np.array([99.5, 94.0, 95.0, 96.0, 97.0, 98.0])
    closes = np.array([100.0, 95.0, 95.5, 97.0, 97.5, 98.5])
    cfg = StrategyConfig(stop_mode="mid_range", do_pyramid=False, breakeven_at_r=0.5,
                         breakeven_offset_points=3.0)
    L = structure_levels(highs, lows, cfg.k)
    positions, _ = _simulate_leg(highs, lows, closes, L, 0, 100.0, False,
                                 0.0, 110.0, cfg, buffer=0)
    p = positions[0]
    assert p["be_moved"] is True
    assert p["stop"] == 97.0                        # entry 100 - 3pt
    assert p["status"] == "stop" and p["exit"] == 97.0


def test_stop0_can_sit_on_the_far_side_of_entry_under_mid_range():
    """ALGODEV-40, found live 2026-09-07: under stop_mode="mid_range" every
    position in a leg -- primary AND every pyramided add (both go through
    the SAME pick_stop() call, see _simulate_leg's add branch above) --
    shares ONE common stop (range_stop) regardless of that position's own
    entry price. A pyramided add entered on a pullback below range_stop has
    its (correct) shared stop ABOVE its own entry -- reproduced here via a
    primary entry with the same geometry, since pick_stop()'s behavior is
    identical for both. stop0 must reflect that real value exactly, not a
    direction-based mirror of risk0 (which would silently place it BELOW
    entry instead, on the wrong side -- exactly what caused two live adds
    to lose money on trades the validated engine says should have won)."""
    n = 5
    highs = np.full(n, 100.0)
    lows = np.full(n, 99.0)
    closes = np.full(n, 99.5)
    cfg = StrategyConfig(stop_mode="mid_range", do_pyramid=False)
    L = structure_levels(highs, lows, cfg.k)

    # entry (99.2) is BELOW the shared range_stop (99.5) for a long --
    # exactly the geometry that broke live: stop sits above entry.
    up_positions, _ = _simulate_leg(highs, lows, closes, L, 0, 99.2, True,
                                    105.0, 99.5, cfg, buffer=0)
    p = up_positions[0]
    assert p["stop"] == 99.5
    assert p["stop0"] == 99.5           # real shared stop, above entry
    assert p["stop0"] != p["entry"] - p["risk0"]  # NOT the risk0-mirrored (wrong-side) guess


def test_b_reversal_recovery_leg_carries_its_own_target_not_the_primary_legs():
    """End-to-end through simulate_day(): a failed B breakout that reverses
    to mid and flips into an A-style trade in the OPPOSITE direction must
    tag its own positions with the recovery target (tp_A), never the
    primary leg's tp -- that mismatch is exactly what cTrader's
    TRADING_BAD_STOPS rejected live."""
    n = 180
    idx = pd.date_range("2026-08-13 08:00", periods=n, freq="1min")
    highs = np.full(n, 100.0)
    lows = np.full(n, 99.0)
    opens = np.full(n, 99.5)
    closes = np.full(n, 99.5)

    # Frankfurt range 08:00-08:59 (bars 0-59): rh=100, rl=99, mid=99.5, height=1.
    # Primary B setup (up) breaks above rh shortly after, then fails and
    # reverts to mid without reaching its target -- triggering b_reversal_to_A.
    e_idx = 65
    opens[e_idx] = 99.6
    closes[e_idx] = 100.2   # confirmation candle breaks above rh=100 -> up B
    for t in range(e_idx + 1, e_idx + 10):
        highs[t] = 100.3
        lows[t] = 99.9
        closes[t] = 100.0
    rev_idx = e_idx + 10
    closes[rev_idx] = 99.5   # falls back to mid -> reversal to A, down direction
    lows[rev_idx] = 99.4

    df = pd.DataFrame(dict(open=opens, high=highs, low=lows, close=closes), index=idx)
    df["date_only"] = df.index.date
    df["time_only"] = df.index.time

    cfg = StrategyConfig(stop_mode="mid_range", tp_mode="range", do_pyramid=False,
                         b_reversal_to_A=True)
    bars = df[(df["time_only"] >= pd.Timestamp("08:00").time())
             & (df["time_only"] <= pd.Timestamp("11:00").time())]
    result = simulate_day(bars, rh=100.0, rl=99.0, mid=99.5, height=1.0, lv={}, cfg=cfg)

    if result.get("scenario") != "B" or not result.get("positions"):
        return   # setup didn't form the way this synthetic series intended; not what's under test

    primary_tp = result["tp"]
    recovery = [p for p in result["positions"] if p.get("is_recovery")]
    if not recovery:
        return

    for p in recovery:
        assert "tp" in p, "recovery-leg position missing its own tp field"
        assert p["tp"] != primary_tp, (
            "recovery leg fell back to the primary leg's tp -- the exact "
            "TRADING_BAD_STOPS regression")
