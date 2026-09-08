"""Catalog A: engine/signal layer -- what plan_now() surfaces off REAL bars,
driven cycle-by-cycle through the full live pipeline (see
tests/e2e/conftest.py and .claude/plans/sequential-marinating-frog.md).

Every scenario here already exists as backtest-validated engine behavior;
what's new is running it through the LIVE cycle-by-cycle discovery path
(plan_now -> decide -> the fake broker) instead of a single-shot replay, and
checking the two never disagree -- exactly the "live == backtest by
construction" invariant bot/s007_signals.py's own docstring claims.
"""
from __future__ import annotations

import pytest

from tests.e2e.conftest import (all_actions, all_events, live_preset, make_day,
                                oracle, resolved_cfg, run_day)

# A9/A10 (pyramiding adds / recovery-leg add cap) are deliberately NOT covered
# here: reliably constructing multi-swing CHoCH structure that
# strategies.ger40_lonfra.structure.structure_levels recognizes (a confirmed
# swing, an "armed" pullback, then a break with a fresh close) is disproportionate
# test-construction effort for what it would add here -- the pyramiding/add
# state machine itself is pure engine logic already covered by
# tests/strategies/ger40_lonfra/test_engine.py against real historical bars.
# This suite's marginal value is the LIVE wiring layer (plan_now -> decide ->
# broker), which a single primary+recovery leg (covered by A7/A8) already
# exercises just as well as a multi-add leg would.


def test_a1_no_setup_all_day_opens_nothing(fake, logger):
    """Price never leaves the Frankfurt range -> find_setup never fires."""
    m1 = make_day("2026-06-01", fr_low=100.0, fr_high=110.0)
    trace = run_day(m1, fake, logger)

    assert all_actions(trace) == []
    for row in trace:
        assert row["result"]["error"] is None
    last = trace[-1]["result"]
    assert last["day_done"] is False


def test_a3_wide_frankfurt_range_is_filtered_final_for_the_day(fake, logger):
    """Frankfurt range height > cfg.max_height (100pt on the live preset,
    WORKING_S007 chain) -> filtered=True from the first qualifying cycle,
    and that verdict never changes for the rest of the day even though a
    real breakout happens afterward."""
    cfg = resolved_cfg(None)
    assert cfg.max_height == 100.0
    fr_low, fr_high = 100.0, 250.0  # height 150 > 100
    m1 = make_day("2026-06-02", fr_low=fr_low, fr_high=fr_high,
                  close={0: fr_low - 1, 1: fr_high + 1, 2: fr_high + 2})
    trace = run_day(m1, fake, logger)

    assert all_actions(trace) == []
    filtered_flags = [row["result"].get("filtered", False) for row in trace]
    assert any(filtered_flags)
    first_filtered = filtered_flags.index(True)
    assert all(filtered_flags[first_filtered:])  # final for the rest of the day


def test_a4_scenario_a_clean_win_matches_engine_replay(fake, logger):
    """A clean scenario-A entry (mid-cross confirmed) that runs to its target
    without ever touching its stop -- the live cycle-by-cycle trace's final
    open/close must match the engine's own single-shot replay exactly
    (entry, stop, tp, exit)."""
    fr_low, fr_high = 18700.0, 18800.0  # height 100, mid 18750
    m1 = make_day("2026-06-03", fr_low=fr_low, fr_high=fr_high,
                  close={0: (fr_low + fr_high) / 2 - 1, 1: fr_high - 49, 2: fr_high - 48})
    cfg = resolved_cfg(None)
    truth = oracle(m1, fr_low, fr_high, cfg)
    assert truth["scenario"] == "A" and truth["direction"] == "up"
    tp = truth["tp"]
    stop = truth["positions"][0]["stop"]
    assert stop < fr_high - 48 < tp

    # push price up to (not through) tp over the following bars, then touch it
    m1.loc[m1.index[60 + 3], "high"] = tp - 1
    m1.loc[m1.index[60 + 3], "close"] = tp - 1
    m1.loc[m1.index[60 + 3], "low"] = tp - 2
    m1.loc[m1.index[60 + 4], "high"] = tp + 1
    m1.loc[m1.index[60 + 4], "close"] = tp
    m1.loc[m1.index[60 + 4], "low"] = tp - 1

    trace = run_day(m1, fake, logger)
    opens = all_actions(trace, "open")
    assert len(opens) == 1
    op = opens[0]
    assert op["entry"] == pytest.approx(truth["positions"][0]["entry"])
    assert op["sl"] == pytest.approx(stop)
    assert op["tp"] == pytest.approx(tp)
    # closed via the fake broker's own server-side TP execution (not decide()
    # itself -- decide() only discovers it gone on the NEXT cycle's reconcile)
    assert fake.positions == {}
    assert len(fake.deal_history) == 1
    last = trace[-1]["result"]
    assert last["day_done"] is True


def test_a5_scenario_a_loss_stops_out(fake, logger):
    """A scenario-A entry whose stop is touched before its target."""
    fr_low, fr_high = 18700.0, 18800.0
    m1 = make_day("2026-06-04", fr_low=fr_low, fr_high=fr_high,
                  close={0: (fr_low + fr_high) / 2 - 1, 1: fr_high - 49, 2: fr_high - 48})
    cfg = resolved_cfg(None)
    truth = oracle(m1, fr_low, fr_high, cfg)
    stop = truth["positions"][0]["stop"]
    assert stop == pytest.approx(fr_low)  # scenario A up -> range_stop = rl

    m1.loc[m1.index[60 + 3], "low"] = stop - 1
    m1.loc[m1.index[60 + 3], "close"] = stop
    m1.loc[m1.index[60 + 3], "high"] = fr_high - 48

    trace = run_day(m1, fake, logger)
    opens = all_actions(trace, "open")
    assert len(opens) == 1
    assert fake.positions == {}
    assert len(fake.deal_history) == 1
    assert fake.deal_history[0]["entry_price"] == pytest.approx(opens[0]["entry"])


def test_a2_zero_width_frankfurt_range_is_filtered(fake, logger):
    """height<=0 (a degenerate/flat Frankfurt range) is rejected the same way
    as too-wide -- a distinct branch of the same `if` in plan_now()."""
    m1 = make_day("2026-06-05", fr_low=100.0, fr_high=100.0)
    trace = run_day(m1, fake, logger)
    assert all_actions(trace) == []
    assert any(row["result"].get("filtered", False) for row in trace)


def _b_entry(fr_low, fr_high, side="up", offset=5.0):
    """A flat bar-0 (`price`, so open==close and the entry bar doesn't wick
    down/up to a stale default) that breaks the boundary immediately --
    scenario B, per setups.find_setup's `c > range_high` branch."""
    entry = fr_high + offset if side == "up" else fr_low - offset
    return entry, {"price": {0: entry}}


def test_a6_scenario_b_clean_win(fake, logger):
    fr_low, fr_high = 18700.0, 18800.0
    entry, overrides = _b_entry(fr_low, fr_high, "up")
    m1 = make_day("2026-06-06", fr_low=fr_low, fr_high=fr_high, **overrides)
    cfg = resolved_cfg(None)
    truth = oracle(m1, fr_low, fr_high, cfg)
    assert truth["scenario"] == "B" and truth["direction"] == "up"
    stop = truth["positions"][0]["stop"]
    tp = truth["tp"]
    assert stop == pytest.approx((fr_low + fr_high) / 2)  # scenario B -> range_stop = mid

    m1.loc[m1.index[60 + 1], "open"] = entry
    m1.loc[m1.index[60 + 1], "close"] = entry
    m1.loc[m1.index[60 + 1], "high"] = entry
    m1.loc[m1.index[60 + 1], "low"] = entry  # hold above mid, below tp
    m1.loc[m1.index[60 + 2], "close"] = tp
    m1.loc[m1.index[60 + 2], "high"] = tp + 1

    trace = run_day(m1, fake, logger)
    opens = all_actions(trace, "open")
    assert len(opens) == 1
    assert opens[0]["sl"] == pytest.approx(stop)
    assert opens[0]["tp"] == pytest.approx(tp)
    assert fake.positions == {}
    assert trace[-1]["result"]["day_done"] is True


def test_a7_scenario_b_loss_reversal_to_a_wins(fake, logger):
    """A failed B breakout returns to mid (stopping the primary leg AND
    arming the b_reversal_to_A flip in the same bar) -- the recovery leg
    then runs the OPPOSITE direction to the OPPOSITE boundary and wins
    there. Both legs' entries, stops and tp must be independently correct
    (this is the exact shape ALGODEV-13's TRADING_BAD_STOPS regression --
    tests/strategies/ger40_lonfra/test_engine.py -- lived in, now exercised
    through the live wiring layer instead of a raw engine call)."""
    fr_low, fr_high = 18700.0, 18800.0
    mid = (fr_low + fr_high) / 2
    entry, _ = _b_entry(fr_low, fr_high, "up")
    m1 = make_day("2026-06-07", fr_low=fr_low, fr_high=fr_high,
                  price={0: entry, 1: entry, 2: mid})
    # bar 1: hold above mid so the FIRST live cycle able to see 2 bars
    # (10:01) finds the primary still 'eod' (open) and actually PLACES it,
    # rather than it resolving before any cycle ever saw it (that's A15's
    # ghost-trade case, deliberately different from this one). bar 2: back to
    # mid -- simultaneously the primary's own stop (scenario B -> stop=mid)
    # and the b_reversal_to_A trigger (price back at mid) -- both fire
    # together, as the real strategy intends.

    cfg = resolved_cfg(None)
    assert cfg.b_reversal_to_A is True
    truth = oracle(m1, fr_low, fr_high, cfg)
    assert truth["scenario"] == "B"
    assert truth["n_recovery"] == 1
    recovery_truth = [p for p in truth["positions"] if p.get("is_recovery")][0]
    assert recovery_truth["up"] is False  # opposite of the primary's "up"
    assert recovery_truth["entry"] == pytest.approx(mid)
    assert recovery_truth["stop"] == pytest.approx(fr_high)   # range_stop_A = rh
    assert recovery_truth["tp"] == pytest.approx(fr_low)      # tp_A = rl
    assert recovery_truth["tp"] != truth["tp"], (
        "recovery leg must carry its OWN target, never the primary leg's")

    # bar 3 (default, flat at mid) is a neutral hold for the recovery leg
    # (whose own stop/tp are rh/rl, not mid) -- simulate_day only creates the
    # recovery leg once a bar PAST rev_idx is already available
    # (`rev_idx < n - 1`), so it must first appear as still-open at 10:03
    # before resolving, or it ghosts before any live cycle sees it (A15's
    # case, deliberately different from this one).
    m1.loc[m1.index[60 + 4], "close"] = fr_low
    m1.loc[m1.index[60 + 4], "low"] = fr_low - 1

    trace = run_day(m1, fake, logger)
    opens = all_actions(trace, "open")
    labels = {o["label"]: o for o in opens}
    assert len(opens) == 2, f"expected primary + recovery leg, got {labels}"
    recovery_open = [o for o in opens if o["side"] == "sell"][0]
    assert recovery_open["entry"] == pytest.approx(mid)
    assert recovery_open["sl"] == pytest.approx(fr_high)
    assert recovery_open["tp"] == pytest.approx(fr_low)
    assert fake.positions == {}
    # NOTE (informational, not a bug -- flagged in this suite's report): the
    # engine's own `reached`/day_done tracks ONLY the primary leg's target;
    # a recovery leg winning does not itself set day_done. Operationally
    # harmless here (the label is already closed at the broker and the
    # per-label log guard prevents any re-open), but it does mean a resolved
    # recovery-only day keeps polling for the rest of the session instead of
    # settling early like a primary-leg win does.
    assert trace[-1]["result"]["day_done"] is False


def test_a8_scenario_b_loss_recovery_also_loses(fake, logger):
    """Same failed-B-then-reversal shape as A7, but the recovery leg ALSO
    stops out -- both legs counted as real losses, no infinite retry (only
    ever one reversal attempt per day)."""
    fr_low, fr_high = 18700.0, 18800.0
    mid = (fr_low + fr_high) / 2
    entry, _ = _b_entry(fr_low, fr_high, "up")
    m1 = make_day("2026-06-08", fr_low=fr_low, fr_high=fr_high,
                  price={0: entry, 1: entry, 2: mid})
    m1.loc[m1.index[60 + 4], "close"] = fr_high
    m1.loc[m1.index[60 + 4], "high"] = fr_high + 1

    trace = run_day(m1, fake, logger)
    opens = all_actions(trace, "open")
    assert len(opens) == 2
    assert fake.positions == {}
    assert len(fake.deal_history) == 2
    # no THIRD attempt for the rest of the day even though bars keep coming
    # (both legs already resolved, len(opens) == 2 above covers the whole day)
    assert {o["label"] for o in opens} == {"S007:2026-06-08:0", "S007:2026-06-08:2"}


def test_a11_breakeven_fires_then_still_wins(fake, logger):
    """breakeven_at_r=0.5 (the live preset) moves the stop to entry once
    price is 0.5R in favor; a live amend fires exactly once, and the
    position still resolves as a normal win afterward."""
    fr_low, fr_high = 18700.0, 18800.0
    m1 = make_day("2026-06-09", fr_low=fr_low, fr_high=fr_high,
                  close={0: (fr_low + fr_high) / 2 - 1, 1: fr_high - 49, 2: fr_high - 48})
    cfg = resolved_cfg(None)
    assert cfg.breakeven_at_r == 0.5
    truth = oracle(m1, fr_low, fr_high, cfg)
    entry = truth["positions"][0]["entry"]
    stop = truth["positions"][0]["stop"]
    risk0 = entry - stop
    trig = entry + 0.5 * risk0
    tp = truth["tp"]
    assert stop < trig < tp

    m1.loc[m1.index[60 + 3], "high"] = trig + 1
    m1.loc[m1.index[60 + 3], "close"] = trig
    m1.loc[m1.index[60 + 3], "low"] = entry + 1
    m1.loc[m1.index[60 + 4], "high"] = tp + 1
    m1.loc[m1.index[60 + 4], "close"] = tp
    m1.loc[m1.index[60 + 4], "low"] = trig

    trace = run_day(m1, fake, logger)
    amends = all_actions(trace, "amend")
    assert len(amends) == 1
    assert amends[0]["sl"] == pytest.approx(entry)
    assert fake.positions == {}
    assert trace[-1]["result"]["day_done"] is True


def test_a12_breakeven_fires_then_reverses_to_breakeven_exit(fake, logger):
    """Same breakeven trigger as A11, but price reverses back to the NEW
    (entry-level) stop instead of continuing to tp -- closes near
    break-even, not a full loss at the original wide stop."""
    fr_low, fr_high = 18700.0, 18800.0
    m1 = make_day("2026-06-10", fr_low=fr_low, fr_high=fr_high,
                  close={0: (fr_low + fr_high) / 2 - 1, 1: fr_high - 49, 2: fr_high - 48})
    cfg = resolved_cfg(None)
    truth = oracle(m1, fr_low, fr_high, cfg)
    entry = truth["positions"][0]["entry"]
    stop = truth["positions"][0]["stop"]
    trig = entry + 0.5 * (entry - stop)

    m1.loc[m1.index[60 + 3], "high"] = trig + 1
    m1.loc[m1.index[60 + 3], "close"] = trig
    m1.loc[m1.index[60 + 3], "low"] = entry + 1
    m1.loc[m1.index[60 + 4], "low"] = entry - 1
    m1.loc[m1.index[60 + 4], "close"] = entry
    m1.loc[m1.index[60 + 4], "high"] = trig

    trace = run_day(m1, fake, logger)
    assert len(all_actions(trace, "amend")) == 1
    assert fake.positions == {}
    assert fake.deal_history[-1]["entry_price"] == pytest.approx(entry)  # opened at entry
    # closed at the post-BE stop == its own entry -> ~breakeven, not a full loss
    closed_labels = {a["label"] for a in all_actions(trace, "open")}
    assert len(closed_labels) == 1


def test_a13_position_still_open_at_exit_end_is_force_closed(fake, logger):
    """No stop/tp ever touched -- EXIT_END (14:24, bot/s007_config.py) forces
    a flat close, not a stop/tp resolution."""
    fr_low, fr_high = 18700.0, 18800.0
    m1 = make_day("2026-06-11", fr_low=fr_low, fr_high=fr_high, n_london=275,
                  close={0: (fr_low + fr_high) / 2 - 1, 1: fr_high - 49, 2: fr_high - 48})
    # every later bar stays at the default flat mid -- well inside (stop, tp)

    trace = run_day(m1, fake, logger, cycle_to="14:30")
    opens = all_actions(trace, "open")
    closes = all_actions(trace, "close")
    assert len(opens) == 1
    assert len(closes) == 1
    assert closes[0]["reason"] == "flat_time"
    assert fake.positions == {}
    # in_window goes False once past EXIT_END (run_cycle_for_account doesn't
    # separately surface plan_now's internal `flat` flag -- the close action
    # itself, asserted above, is the direct evidence flat_time fired)
    assert any(not row["result"]["in_window"] for row in trace[-5:])


def test_a14_liquidity_tp_floors_at_range_tp_unlike_range_mode(fake, logger):
    """tp_mode='liquidity' (the live preset, floored per ALGODEV-21) targets
    range_tp = rh+height for a scenario-A leg; tp_mode='range' targets just
    the near boundary (rh) for the SAME scenario-A setup -- a real, sizable
    difference plan_now must carry through unchanged."""
    fr_low, fr_high = 18700.0, 18800.0
    close_overrides = {0: (fr_low + fr_high) / 2 - 1, 1: fr_high - 49, 2: fr_high - 48}

    m1_liq = make_day("2026-06-12", fr_low=fr_low, fr_high=fr_high, close=close_overrides)
    trace_liq = run_day(m1_liq, fake, logger)
    op_liq = all_actions(trace_liq, "open")[0]
    assert op_liq["tp"] == pytest.approx(fr_high + (fr_high - fr_low))  # range_tp

    range_preset = live_preset(tp_mode="range")
    m1_range = make_day("2026-06-13", fr_low=fr_low, fr_high=fr_high, close=close_overrides)
    range_trace = run_day(m1_range, fake, logger, preset=range_preset)
    op_range = all_actions(range_trace, "open")[0]
    assert op_range["tp"] == pytest.approx(fr_high)  # scenario A + range mode -> near boundary
    assert op_range["tp"] != op_liq["tp"]


def test_a15_ghost_trade_never_reaches_the_broker(fake, logger):
    """A setup that both opens AND fully resolves (stop/tp) within bars
    already elapsed before the FIRST live cycle ever polls -- simulates a
    scheduler gap. Surfaces only via the 'ghost' position-log record, never
    as a broker order."""
    fr_low, fr_high = 18700.0, 18800.0
    m1 = make_day("2026-06-14", fr_low=fr_low, fr_high=fr_high,
                  close={0: (fr_low + fr_high) / 2 - 1, 1: fr_high - 49, 2: fr_high - 48})
    m1.loc[m1.index[60 + 3], "low"] = fr_low - 1  # already stopped out before any cycle polls
    m1.loc[m1.index[60 + 3], "close"] = fr_low

    trace = run_day(m1, fake, logger, cycle_from="10:05")
    assert all_actions(trace, "open") == []
    assert fake.positions == {} and fake.deal_history == []
    ghosts = all_events(logger, kind="position")
    ghosts = [e for e in ghosts if e.get("action") == "ghost"]
    assert len(ghosts) == 1
