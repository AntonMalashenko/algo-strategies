"""bot/orb_signals.py::decide() state machine, exercised end-to-end through
run_cycle_for_account() against a tiny in-process fake broker (CTraderORB is
monkeypatched out -- no real cTrader connection). Covers every state listed
in decide()'s module docstring: place both resting stops, one leg fills
(cancel the sibling, log the fill), time exit, a broker-side stop-out
backfill (and no re-entry after it), and a cutoff cancel with no fill (and
no re-entry after that either).

Flat synthetic data (open=20000, session range exactly 100 pts every prior
day) makes U/L/stop distance fixed and easy to assert against: k=0.20 ->
U=20020/L=19980, stop_adr_mult=0.75 -> stop=75pts from each order's own
entry. The prior sessions are served as M15 bars (the ADR14 history window,
_make_m15) and today as M1 bars (_make_m1), matching the two trendbar
fetches CTraderORB.run_live_cycle_orb now makes per cycle.
"""
from __future__ import annotations

import json
from unittest.mock import patch

import pandas as pd
import pytest

from bot import orb_config as C
from bot.orb_signals import run_cycle_for_account
from utils.trade_logger import StrategyLogger

U = 20000.0 + C.STRATEGY.k_range * 100.0    # 20020.0
L = 20000.0 - C.STRATEGY.k_range * 100.0    # 19980.0
STOP_DIST = C.STRATEGY.stop_adr_mult * 100.0  # 75.0


N_PRIOR_SESSIONS = 16  # > adr_window (14), so ADR14 is computable on `day`


def _prior_session_rows(day: str, freq: str, n_prior: int = N_PRIOR_SESSIONS) -> list[dict]:
    """`n_prior` flat full sessions (open=20000, range=100pts, so ADR14=100)
    on the first weekdays walking forward from `day` - 40 days, strictly
    before `day`, as bars of `freq` ("1min" or "15min")."""
    cfg = C.STRATEGY
    rows = []
    day_ts = pd.Timestamp(day)
    d = day_ts - pd.Timedelta(days=40)
    n_done = 0
    while n_done < n_prior and d.date() < day_ts.date():
        if d.dayofweek < 5:
            open_ts = pd.Timestamp.combine(d.date(), cfg.session_open)
            close_ts = pd.Timestamp.combine(d.date(), cfg.session_close)
            for ts in pd.date_range(open_ts, close_ts, freq=freq):
                rows.append(dict(ts=ts, open=20000.0, high=20050.0, low=19950.0, close=20000.0))
            n_done += 1
        d += pd.Timedelta(days=1)
    return rows


def _make_m1(day: str, upto: str) -> pd.DataFrame:
    """16 flat prior full sessions (open=20000, range=100pts, so ADR14=100)
    strictly before `day`, plus `day` itself up to `upto` ("HH:MM")."""
    cfg = C.STRATEGY
    rows = _prior_session_rows(day, "1min")
    day_ts = pd.Timestamp(day)

    open_ts = pd.Timestamp.combine(day_ts.date(), cfg.session_open)
    upto_ts = pd.Timestamp.combine(day_ts.date(), pd.to_datetime(upto, format="%H:%M").time())
    for ts in pd.date_range(open_ts, upto_ts, freq="1min"):
        rows.append(dict(ts=ts, open=20000.0, high=20005.0, low=19995.0, close=20000.0))

    return pd.DataFrame(rows).set_index("ts").sort_index()


def _make_m15(day: str, n_prior: int = N_PRIOR_SESSIONS) -> pd.DataFrame:
    """The ADR14 history window as M15 bars: the same flat prior sessions
    _make_m1 builds (default 16, range=100pts, so ADR14=100), strictly
    before `day`. Today's own bars are not needed -- bot/orb_signals.py::
    _valid_prior_sessions only reads sessions strictly before today."""
    return pd.DataFrame(_prior_session_rows(day, "15min", n_prior)).set_index("ts").sort_index()


class _FakeAPI:
    """Stands in for CTraderORB: calls decide() against a scripted broker
    state (`self.state`) instead of a real cTrader session. Records
    whatever decide() actually returned into state["dispatched_actions"] --
    this is what the broker-mode gate tests assert on, since it is exactly
    the set of actions that would have been sent to the real broker (a
    resting place_stop never reaches run_cycle_for_account's own `actions`
    return value either way -- only a fill/close does, see that function's
    docstring -- so the gate can only be observed at this dispatch point)."""
    def __init__(self, state, creds=None):
        self.state = state

    def run_live_cycle_orb(self, symbol_candidates, history_days, today_days, decide):
        st = self.state
        st["fetch_windows"] = dict(history_days=history_days, today_days=today_days)
        actions = decide("US100", st["m1"], st["m15"], st["positions"], st["orders"],
                         st["balance"], st["money_per_point_per_lot"])
        st["dispatched_actions"] = actions
        results = [dict(action=a, result="ok", error=None) for a in actions]
        return dict(symbol="US100", m1=st["m1"], m15=st["m15"], positions=st["positions"],
                    orders=st["orders"], actions=actions, results=results,
                    balance=st["balance"], money_per_point_per_lot=st["money_per_point_per_lot"])


@pytest.fixture
def broker(tmp_path):
    """A scripted broker state plus a run_cycle() helper bound to a fresh
    StrategyLogger under tmp_path (so label_was_opened/closed reflect only
    this test). run_cycle() defaults to broker="off" (the safe default) --
    pass broker_mode="execute"/"dry" explicitly where a test needs it."""
    state = {"m1": None, "m15": None, "positions": [], "orders": [], "balance": 10_000.0,
            "money_per_point_per_lot": 1.0, "dispatched_actions": None,
            "fetch_windows": None}
    logger = StrategyLogger("S021-test", log_root=str(tmp_path), console=False)

    def run_cycle(broker_mode="off"):
        with patch("bot.ctrader_orb.CTraderORB", lambda creds=None: _FakeAPI(state)):
            return run_cycle_for_account(None, logger=logger, symbol_candidates=["US100"],
                                         history_days=30, broker=broker_mode)

    return state, run_cycle, logger


def test_happy_path_place_fill_cancel_sibling_time_exit(broker):
    state, run_cycle, logger = broker
    day = "2026-09-08"
    long_label, short_label = f"S021:{day}:long", f"S021:{day}:short"

    state["m1"] = _make_m1(day, "09:30")
    state["m15"] = _make_m15(day)
    r1 = run_cycle()
    assert r1["actions"] == []  # just placed the two resting stops, no DB action yet

    # long leg fills
    state["m1"] = _make_m1(day, "10:15")
    state["positions"] = [dict(position_id=1, label=long_label, side="buy", volume=100,
                               price=U, stop_loss=U - STOP_DIST)]
    state["orders"] = [dict(order_id=99, label=short_label, side="sell", stop_price=L,
                            stop_loss=L + STOP_DIST)]
    r2 = run_cycle()
    assert [a["kind"] for a in r2["actions"]] == ["open"]
    assert r2["actions"][0]["label"] == long_label
    assert r2["actions"][0]["side"] == "buy"

    # sibling order cancelled broker-side by now
    state["orders"] = []
    state["m1"] = _make_m1(day, "12:00")
    r3 = run_cycle()
    assert r3["actions"] == []  # already logged, nothing new mid-day

    # time exit
    state["m1"] = _make_m1(day, "16:00")
    r4 = run_cycle()
    assert [a["kind"] for a in r4["actions"]] == ["close"]
    assert r4["actions"][0]["reason"] == "time"

    # broker confirms closed -> no re-entry for the rest of the day
    state["positions"] = []
    r5 = run_cycle()
    assert r5["actions"] == []


def test_broker_side_stop_out_is_backfilled_and_blocks_reentry(broker):
    state, run_cycle, logger = broker
    day = "2026-09-09"
    long_label = f"S021:{day}:long"

    state["m1"] = _make_m1(day, "09:30")
    state["m15"] = _make_m15(day)
    run_cycle()

    state["m1"] = _make_m1(day, "10:00")
    state["positions"] = [dict(position_id=2, label=long_label, side="buy", volume=100,
                               price=U, stop_loss=U - STOP_DIST)]
    state["orders"] = []
    r2 = run_cycle()
    assert [a["kind"] for a in r2["actions"]] == ["open"]

    # SL fires broker-side between cycles -- we never sent a close ourselves
    state["positions"] = []
    state["m1"] = _make_m1(day, "10:30")
    r3 = run_cycle()
    assert [a["kind"] for a in r3["actions"]] == ["close"]
    assert r3["actions"][0]["reason"] == "stop_broker_side"

    # still within the entry window, but must NOT re-enter the same day
    state["m1"] = _make_m1(day, "11:00")
    r4 = run_cycle()
    assert r4["actions"] == []


def test_cutoff_cancel_with_no_fill_blocks_reentry(broker):
    state, run_cycle, logger = broker
    day = "2026-09-10"
    long_label, short_label = f"S021:{day}:long", f"S021:{day}:short"

    state["m1"] = _make_m1(day, "09:30")
    state["m15"] = _make_m15(day)
    run_cycle()

    state["orders"] = [dict(order_id=1, label=long_label, side="buy", stop_price=U, stop_loss=0),
                       dict(order_id=2, label=short_label, side="sell", stop_price=L, stop_loss=0)]
    state["m1"] = _make_m1(day, "14:35")  # entry_cutoff is 14:29
    r1 = run_cycle()
    assert r1["actions"] == []  # cancels are order-level, never a DB Position action

    state["orders"] = []
    state["m1"] = _make_m1(day, "15:00")
    r2 = run_cycle()
    assert r2["actions"] == []


def test_both_legs_filled_anomaly_flattens_both(broker):
    state, run_cycle, logger = broker
    day = "2026-09-11"
    long_label, short_label = f"S021:{day}:long", f"S021:{day}:short"

    state["m1"] = _make_m1(day, "09:30")
    state["m15"] = _make_m15(day)
    run_cycle()

    state["m1"] = _make_m1(day, "10:05")
    state["positions"] = [
        dict(position_id=3, label=long_label, side="buy", volume=100, price=U, stop_loss=U - STOP_DIST),
        dict(position_id=4, label=short_label, side="sell", volume=100, price=L, stop_loss=L + STOP_DIST),
    ]
    state["orders"] = []
    r1 = run_cycle()
    kinds = [a["kind"] for a in r1["actions"]]
    assert kinds.count("open") == 2
    assert kinds.count("close") == 2
    reasons = {a["reason"] for a in r1["actions"] if a["kind"] == "close"}
    assert reasons == {"both_filled_anomaly"}


def _events(logger, kind):
    out = []
    for f in sorted(logger.dir.glob("events-*.jsonl")):
        out += [r for r in map(json.loads, f.read_text().splitlines()) if r["kind"] == kind]
    return out


def test_fetch_windows_reach_the_broker_adapter(broker):
    # history_days is the M15 ADR-history window; today_days (left to its
    # default here) is the small M1 "today" window.
    state, run_cycle, _logger = broker
    day = "2026-09-14"
    state["m1"] = _make_m1(day, "09:30")
    state["m15"] = _make_m15(day)
    run_cycle()
    assert state["fetch_windows"] == dict(history_days=30, today_days=C.TODAY_M1_DAYS)


def test_short_adr_history_with_valid_anchor_logs_warning_event(broker):
    # The original live bug's signature (valid 09:30 anchor, < adr_window
    # valid prior sessions) must surface as its own event, not only as
    # has_levels=False in "state".
    state, run_cycle, logger = broker
    day = "2026-09-15"
    cfg = C.STRATEGY
    state["m1"] = _make_m1(day, "09:45")
    state["m15"] = _make_m15(day, n_prior=cfg.adr_window - 1)
    r = run_cycle(broker_mode="execute")
    assert r["actions"] == [] and state["dispatched_actions"] == []
    (warn,) = _events(logger, "insufficient_adr_history")
    assert warn["sessions_available"] == cfg.adr_window - 1
    assert warn["adr_window"] == cfg.adr_window
    text_log = (logger.dir / f"{logger.strategy}.log").read_text().splitlines()
    assert any("| WARNING |" in ln and f"only {cfg.adr_window - 1} prior M15" in ln
               for ln in text_log), "insufficient_adr_history not logged at WARNING level"
    (st,) = _events(logger, "state")
    assert st["has_levels"] is False
    assert st["sessions_available"] == cfg.adr_window - 1


def test_no_anchor_is_not_reported_as_short_history(broker):
    # No 09:30 bar today (bot started late / data gap) is the normal
    # "nothing to do" case -- it must NOT raise the insufficient-history
    # warning, even though the M15 history here is also short.
    state, run_cycle, logger = broker
    day = "2026-09-16"
    m1 = _make_m1(day, "10:00")
    state["m1"] = m1.drop(pd.Timestamp(f"{day} 09:30"))
    state["m15"] = _make_m15(day, n_prior=C.STRATEGY.adr_window - 1)
    run_cycle(broker_mode="execute")
    assert _events(logger, "insufficient_adr_history") == []
    (st,) = _events(logger, "state")
    assert st["has_levels"] is False
    assert "sessions_available" not in st


def test_enough_adr_history_places_both_stops(broker):
    # Positive counterpart of the warning test above: with adr_window valid
    # M15 sessions the bot does act (execute mode dispatches both entries).
    state, run_cycle, logger = broker
    day = "2026-09-17"
    state["m1"] = _make_m1(day, "09:30")
    state["m15"] = _make_m15(day, n_prior=C.STRATEGY.adr_window)
    run_cycle(broker_mode="execute")
    placed = state["dispatched_actions"]
    assert [(a["kind"], a["side"]) for a in placed] == [("place_stop", "buy"), ("place_stop", "sell")]
    assert placed[0]["stop"] == pytest.approx(U) and placed[1]["stop"] == pytest.approx(L)
    assert placed[0]["sl"] == pytest.approx(U - STOP_DIST)
    assert placed[1]["sl"] == pytest.approx(L + STOP_DIST)
    assert _events(logger, "insufficient_adr_history") == []
