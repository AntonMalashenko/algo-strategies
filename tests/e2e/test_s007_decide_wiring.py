"""Catalog B: decide()/sizing/day-cap layer -- live wiring off the REAL
engine output, driven cycle-by-cycle through the full pipeline. See
tests/e2e/conftest.py and .claude/plans/sequential-marinating-frog.md.

B11/B12 (broker-side close race between our own log and the broker's
reconcile snapshot) are deliberately NOT covered here: this harness's fake
broker and decide()'s bar-driven replay stay in lockstep by construction (one
apply_bar per cycle, always immediately before that cycle's decide() call),
so there is no natural way to desynchronize them the way a REAL lagging M1
bar / delayed reconcile can live. That exact race is already covered at the
decide()-level by tests/bot/test_s007_live_sizing.py's
test_live_skips_reopening_a_label_the_log_already_closed and
test_live_backfills_close_and_skips_reopen_on_broker_side_stop_before_log_catches_up
(hand-scripted broker state, which is the right tool for a race that needs
to be forced rather than grown from real bars).
"""
from __future__ import annotations

import pytest

from bot import risk as risk_mod
from tests.e2e.conftest import all_actions, all_events, make_day, oracle, resolved_cfg, run_day

FR_LOW, FR_HIGH = 18700.0, 18800.0
MID = (FR_LOW + FR_HIGH) / 2.0


def _scenario_a_up(date, n_london=None):
    kw = {} if n_london is None else dict(n_london=n_london)
    return make_day(date, fr_low=FR_LOW, fr_high=FR_HIGH,
                    close={0: MID - 1, 1: FR_HIGH - 49, 2: FR_HIGH - 48}, **kw)


def _b_recovery_day(date, risk_pct, daily_risk_cap_pct):
    """The A7 primary+recovery shape -- two independent 'want a position'
    events in one day, used here purely as a vehicle for cap tests (the
    SECOND event is what a day/dollar cap should be able to block)."""
    m1 = make_day(date, fr_low=FR_LOW, fr_high=FR_HIGH, price={0: FR_HIGH + 5, 1: FR_HIGH + 5, 2: MID})
    m1.loc[m1.index[64], "close"] = FR_LOW
    m1.loc[m1.index[64], "low"] = FR_LOW - 1
    return m1


def test_b1_risk_based_sizing_matches_lots_for_risk(fake, logger):
    m1 = _scenario_a_up("2026-07-01")
    trace = run_day(m1, fake, logger, initial_balance=10_000.0)
    op = all_actions(trace, "open")[0]
    stop_distance = abs(op["entry"] - op["sl"])
    risk_amount = 10_000.0 * 0.25 / 100.0
    expected_lot = risk_mod.lots_for_risk(risk_amount, stop_distance,
                                          fake.money_per_point_per_lot, min_lot=0.01)
    assert op["volume_lots"] == pytest.approx(expected_lot)


def test_b2_use_fixed_lot_ignores_stop_distance(fake, logger):
    m1 = _scenario_a_up("2026-07-02")
    # a bigger cap than default -- 0.07 lots at this fixture's ~52pt stop
    # distance costs ~416, comfortably over the default 2% ($200) cap; this
    # test is about fixed-lot SIZING, not the day cap (that's B6/B20).
    trace = run_day(m1, fake, logger, use_fixed_lot=True, fixed_lot=0.07,
                    daily_risk_cap_pct=10.0, initial_balance=10_000.0)
    op = all_actions(trace, "open")[0]
    assert op["volume_lots"] == pytest.approx(0.07)


def test_b3_min_lot_floor_overshoots_nominal_risk_but_still_placed(fake, logger):
    """A stop distance wide enough that risk-based sizing would want LESS
    than the broker's minimum lot -- real risk ends up bigger than the
    nominal risk_amount, but the position is still placed (at the floor),
    not skipped."""
    m1 = _scenario_a_up("2026-07-03")
    trace = run_day(m1, fake, logger, initial_balance=10_000.0)
    op = all_actions(trace, "open")[0]
    stop_distance = abs(op["entry"] - op["sl"])
    risk_amount = 10_000.0 * 0.25 / 100.0
    nominal_lot = risk_amount / (stop_distance * fake.money_per_point_per_lot)
    assert nominal_lot < 0.01, "fixture no longer exercises the floor -- adjust stop_distance"
    assert op["volume_lots"] == pytest.approx(0.01)
    real_risk = 0.01 * stop_distance * fake.money_per_point_per_lot
    assert real_risk > risk_amount


def test_b4_be_moved_on_first_poll_places_with_wide_stop_not_zero(fake, logger):
    """ALGODEV-38 shape at the pipeline level: a scheduler gap means the
    FIRST live cycle to ever see this label already finds it be_moved (the
    replayed price path crossed breakeven before any cycle polled) -- the
    live layer must still place it with the pre-breakeven (wide) stop."""
    m1 = _scenario_a_up("2026-07-04")
    cfg = resolved_cfg(None)
    truth = oracle(m1, FR_LOW, FR_HIGH, cfg)
    entry = truth["positions"][0]["entry"]
    stop = truth["positions"][0]["stop"]
    trig = entry + cfg.breakeven_at_r * (entry - stop)
    m1.loc[m1.index[60 + 3], "high"] = trig + 1
    m1.loc[m1.index[60 + 3], "close"] = trig
    m1.loc[m1.index[60 + 3], "low"] = entry + 1
    # hold safely between the POST-be stop (entry) and tp for a couple more
    # bars -- a default bar (flat at mid, which sits BELOW entry here) would
    # already trip the post-BE stop and fully ghost this position before
    # cycle_from=10:05 ever gets to see it, which is a different case (A15).
    m1.loc[m1.index[60 + 4], "close"] = trig
    m1.loc[m1.index[60 + 4], "high"] = trig
    m1.loc[m1.index[60 + 4], "low"] = trig
    m1.loc[m1.index[60 + 5], "close"] = trig
    m1.loc[m1.index[60 + 5], "high"] = trig
    m1.loc[m1.index[60 + 5], "low"] = trig

    trace = run_day(m1, fake, logger, cycle_from="10:05")  # skip past the gap
    opens = all_actions(trace, "open")
    assert len(opens) == 1
    assert opens[0]["sl"] == pytest.approx(stop)
    assert abs(opens[0]["entry"] - opens[0]["sl"]) > 1.0, (
        "ALGODEV-38 regression: a fresh placement must never use the "
        "already-be_moved (near-zero-distance) stop")


def test_b5_day_position_count_cap_blocks_a_second_valid_signal(fake, logger):
    """risk_pct sized so max_positions_per_day == 1 -- the recovery leg's
    otherwise-valid signal is skipped purely on count, with room left on
    the $ cap."""
    m1 = _b_recovery_day("2026-07-05", risk_pct=2.0, daily_risk_cap_pct=2.0)
    trace = run_day(m1, fake, logger, risk_pct=2.0, daily_risk_cap_pct=2.0,
                    initial_balance=10_000.0)
    opens = all_actions(trace, "open")
    assert len(opens) == 1
    skips = [e for e in all_events(logger, kind="skip_max_positions")]
    assert len(skips) >= 1


def test_b6_dollar_cap_blocks_before_count_cap(fake, logger):
    """daily_risk_cap_pct small enough that the PRIMARY leg alone already
    consumes the whole day budget -- the recovery leg is skipped on the $
    cap even though the count cap (8 at the default risk_pct) has room."""
    # cap sized so the PRIMARY leg's floored min-lot cost (~63) fits, but
    # primary + recovery (~57 more) together don't (~120 > 100).
    m1 = _b_recovery_day("2026-07-06", risk_pct=0.25, daily_risk_cap_pct=1.0)
    trace = run_day(m1, fake, logger, risk_pct=0.25, daily_risk_cap_pct=1.0,
                    initial_balance=10_000.0)
    opens = all_actions(trace, "open")
    assert len(opens) == 1
    skips = all_events(logger, kind="skip_risk_cap")
    assert len(skips) >= 1


def test_b7_closed_risk_persists_after_a_stop_out(fake, logger):
    """spent_risk_today must keep counting a label's risk after it closes --
    not reset to 0 once the position is no longer open."""
    m1 = make_day("2026-07-07", fr_low=FR_LOW, fr_high=FR_HIGH,
                  close={0: MID - 1, 1: FR_HIGH - 49, 2: FR_HIGH - 48})
    m1.loc[m1.index[63], "low"] = FR_LOW - 1
    m1.loc[m1.index[63], "close"] = FR_LOW
    m1.loc[m1.index[63], "high"] = FR_HIGH - 48

    run_day(m1, fake, logger, initial_balance=10_000.0)
    assert fake.positions == {}
    states = all_events(logger, kind="state")
    last = states[-1]
    assert last["closed_risk_today"] > 0
    assert last["spent_risk_today"] == pytest.approx(last["closed_risk_today"])


def test_b8_new_day_does_not_inherit_yesterdays_cap_usage(fake, logger):
    """risk_pct sized so max_positions_per_day == 1 for BOTH days -- day 1's
    single position must not block day 2's from opening."""
    m1_day1 = _scenario_a_up("2026-07-08")
    m1_day2 = _scenario_a_up("2026-07-09")
    kw = dict(risk_pct=2.0, daily_risk_cap_pct=2.0, initial_balance=10_000.0)

    trace1 = run_day(m1_day1, fake, logger, **kw)
    assert len(all_actions(trace1, "open")) == 1

    trace2 = run_day(m1_day2, fake, logger, **kw)
    assert len(all_actions(trace2, "open")) == 1, (
        "day 2's cap accounting must be scoped to day 2's own labels, not "
        "carry over day 1's already-used budget")


def test_b9_and_b19_slipped_fill_price_drives_the_real_risk_accounting(fake, logger):
    """Broker fill differs from the engine's planned entry (slippage) --
    while open, open_risk must use the broker's own reported fill price;
    once closed, closed_risk_today must use the real fill via closed_deals,
    not the planned entry."""
    fake.slippage_points = 5.0
    m1 = make_day("2026-07-10", fr_low=FR_LOW, fr_high=FR_HIGH,
                  close={0: MID - 1, 1: FR_HIGH - 49, 2: FR_HIGH - 48})
    m1.loc[m1.index[63], "low"] = FR_LOW - 1
    m1.loc[m1.index[63], "close"] = FR_LOW
    m1.loc[m1.index[63], "high"] = FR_HIGH - 48

    trace = run_day(m1, fake, logger, initial_balance=10_000.0)
    op = all_actions(trace, "open")[0]
    planned_entry = op["entry"]
    real_fill = fake.deal_history[0]["entry_price"]
    assert real_fill == pytest.approx(planned_entry + 5.0)  # buy -> worse (higher) fill

    states = all_events(logger, kind="state")
    last = states[-1]
    lot = op["volume_lots"]
    real_risk = lot * abs(real_fill - op["sl"]) * fake.money_per_point_per_lot
    nominal_risk = lot * abs(planned_entry - op["sl"]) * fake.money_per_point_per_lot
    assert real_risk != pytest.approx(nominal_risk)
    assert last["closed_risk_today"] == pytest.approx(real_risk)


def test_b10_missing_deal_history_falls_back_to_logged_entry(fake, logger):
    """No matching deal-history row for a closed-today label (a real deal-
    list fetch can fail/miss a same-second close) -- risk still counts,
    using our OWN logged planned entry/sl instead of silently dropping to 0."""
    m1 = make_day("2026-07-11", fr_low=FR_LOW, fr_high=FR_HIGH,
                  close={0: MID - 1, 1: FR_HIGH - 49, 2: FR_HIGH - 48})
    m1.loc[m1.index[63], "low"] = FR_LOW - 1
    m1.loc[m1.index[63], "close"] = FR_LOW
    m1.loc[m1.index[63], "high"] = FR_HIGH - 48

    trace = run_day(m1, fake, logger, initial_balance=10_000.0, cycle_to="10:10")
    op = all_actions(trace, "open")[0]
    assert fake.deal_history  # the close did happen
    fake.deal_history.clear()  # simulate a failed/missing deal-list fetch

    # one more cycle: closed_risk_today must still reflect this label
    run_day(m1.iloc[:m1.index.get_loc(m1.index[m1.index <= "2026-07-11 10:11"][-1]) + 1],
           fake, logger, initial_balance=10_000.0,
           cycle_from="10:11", cycle_to="10:11")
    states = all_events(logger, kind="state")
    last = states[-1]
    expected = op["volume_lots"] * abs(op["entry"] - op["sl"]) * fake.money_per_point_per_lot
    assert last["closed_risk_today"] == pytest.approx(expected)


def test_b13_manual_stop_flattens_regardless_of_day_state(fake, logger):
    m1 = _scenario_a_up("2026-07-12")
    stopped = {"on": False}
    trace = run_day(m1, fake, logger, stop_flag_active=lambda: stopped["on"],
                    cycle_to="10:05")
    assert len(all_actions(trace, "open")) == 1
    stopped["on"] = True
    # one more cycle with the flag now active
    next_ts = m1.index[m1.index > trace[-1]["ts"]][0]
    extra = run_day(m1.loc[:next_ts], fake, logger, stop_flag_active=lambda: stopped["on"],
                    cycle_from=next_ts.strftime("%H:%M"), cycle_to=next_ts.strftime("%H:%M"))
    closes = all_actions(extra, "close")
    assert len(closes) == 1
    assert closes[0]["reason"] == "manual_stop"
    assert fake.positions == {}


def test_b14_before_trade_start_no_orders_and_not_in_window(fake, logger):
    m1 = _scenario_a_up("2026-07-13")
    trace = run_day(m1, fake, logger, cycle_from="09:30", cycle_to="09:59")
    assert all_actions(trace) == []
    assert all(not row["result"]["in_window"] for row in trace)


def _scenario_a_down(date):
    # mirror of _scenario_a_up: open[0] set ABOVE mid so `cur` starts
    # "above" (the up-case gets "below" for free from the default open==mid),
    # then the close pattern crosses to and confirms "below" -> scenario A down.
    return make_day(date, fr_low=FR_LOW, fr_high=FR_HIGH, open_={0: MID + 1},
                    close={0: MID + 1, 1: FR_LOW + 49, 2: FR_LOW + 48})


def test_b15_and_b16_breakeven_amend_fires_once_on_a_short(fake, logger):
    """Short (scenario-A down) position: the amend tightens toward the
    broker's fill from the correct (sell) side, and never repeats once the
    broker's own stop already sits at/beyond breakeven."""
    m1 = _scenario_a_down("2026-07-14")
    cfg = resolved_cfg(None)
    truth = oracle(m1, FR_LOW, FR_HIGH, cfg)
    entry = truth["positions"][0]["entry"]
    stop = truth["positions"][0]["stop"]
    assert stop > entry  # scenario A down -> range_stop = rh, above entry
    trig = entry - cfg.breakeven_at_r * (stop - entry)

    m1.loc[m1.index[60 + 3], "low"] = trig - 1
    m1.loc[m1.index[60 + 3], "close"] = trig
    m1.loc[m1.index[60 + 3], "high"] = entry - 1
    # hold there for a few more cycles -- amend must not repeat
    for rel in (4, 5, 6):
        m1.loc[m1.index[60 + rel], "close"] = trig
        m1.loc[m1.index[60 + rel], "high"] = entry - 1
        m1.loc[m1.index[60 + rel], "low"] = trig - 1

    trace = run_day(m1, fake, logger, cycle_to="10:10")
    amends = all_actions(trace, "amend")
    assert len(amends) == 1
    assert amends[0]["sl"] == pytest.approx(entry)  # tightened to the (unslipped) fill
    assert amends[0]["sl"] < amends[0]["prev_sl"]   # short -> tightening means a LOWER stop


def test_b17_one_erroring_action_does_not_block_a_later_retry(fake, logger):
    m1 = _scenario_a_up("2026-07-15")
    label = "S007:2026-07-15:2"
    fake.error_labels.add(label)
    trace = run_day(m1, fake, logger, cycle_to="10:03")
    assert all_actions(trace, "open") == []
    order_events = [e for e in all_events(logger, kind="order") if not e["ok"]]
    assert len(order_events) >= 1
    assert fake.positions == {}

    fake.error_labels.discard(label)
    next_ts = m1.index[m1.index > trace[-1]["ts"]][0]
    retry = run_day(m1, fake, logger, cycle_from=next_ts.strftime("%H:%M"),
                    cycle_to=next_ts.strftime("%H:%M"))
    opens = all_actions(retry, "open")
    assert len(opens) == 1, "the label must still be retried, not permanently skipped"


def test_b18_broker_session_error_mid_cycle_does_not_crash_and_recovers(fake, logger):
    m1 = _scenario_a_up("2026-07-16")
    third_ts = m1.index[m1.index >= "2026-07-16 10:02"][0]
    fake.raise_next_cycle = True  # fires on the very first cycle (10:00)
    trace = run_day(m1, fake, logger, cycle_to=third_ts.strftime("%H:%M"))
    assert trace[0]["result"]["error"] is not None
    assert all(row["result"]["error"] is None for row in trace[1:])
    # the entry (idx 2, 10:02) still gets placed once the bars needed for it
    # are available -- one bad cycle didn't corrupt or skip anything
    assert len(all_actions(trace, "open")) == 1


def test_b20_min_lot_risk_alone_exceeds_the_remaining_cap(fake, logger):
    """The floored min-lot's REAL $ risk exceeds the ENTIRE day cap (not
    just the nominal risk_amount, unlike B3) -- must be skipped outright,
    not placed just because it's "only the minimum lot"."""
    # risk_pct kept small enough that max_positions_per_day stays well above
    # 0 (else the COUNT cap masks the $ cap being under test here -- found
    # while building this fixture: daily_risk_cap_pct/risk_pct floors to 0
    # positions/day at very low ratios, tripping skip_max_positions first).
    m1 = _scenario_a_up("2026-07-17")
    trace = run_day(m1, fake, logger, initial_balance=10_000.0,
                    risk_pct=0.01, daily_risk_cap_pct=0.05)
    assert all_actions(trace, "open") == []
    skips = all_events(logger, kind="skip_risk_cap")
    assert len(skips) >= 1
