"""Catalog C: runner/DB layer -- webapp/runner.py::_worker_s007 driven
cycle-by-cycle through the REAL run_cycle_for_account/decide/engine chain
(only bot.ctrader_s007.CTraderS007 is faked, same boundary as catalogs A/B).
Unlike tests/webapp/test_runner_s007_settle.py, run_s007_cycle itself is NOT
monkeypatched here -- that's exactly the seam this suite exists to exercise.
"""
from __future__ import annotations

import datetime
import os
import sys
import types

os.environ.setdefault("APP_SECRET_KEY", "test-only-not-a-real-key")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import webapp.runner as runner
from webapp.db import Base
from webapp.models import Account, AccountStrategy, Broker, Position, Strategy, User

from tests.e2e.conftest import FakeCTraderS007E2E, make_day

FR_LOW, FR_HIGH = 18700.0, 18800.0
MID = (FR_LOW + FR_HIGH) / 2.0


@pytest.fixture(autouse=True)
def _isolate_logs(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "ROOT", tmp_path)
    monkeypatch.setattr(runner, "_sync_after_cycle", lambda *a, **k: None)


@pytest.fixture
def engine():
    e = create_engine("sqlite:///:memory:", future=True,
                      poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(e)
    return e


@pytest.fixture
def Session(engine):
    return sessionmaker(bind=engine, future=True)


def _make_link(Session, *, external_account_id="1", risk_pct=0.25, fixed_lot=0.01,
              use_fixed_lot=False, initial_balance=10_000.0):
    s = Session()
    u = User(username=f"t{external_account_id}", password_hash="x", is_admin=True)
    broker = Broker(name=f"IC Markets {external_account_id}", platforms="CTRADER")
    s.add_all([u, broker])
    s.flush()
    acc = Account(user_id=u.id, broker="CTRADER", broker_id=broker.id,
                 external_account_id=external_account_id, env="demo",
                 label=f"demo{external_account_id}")
    strat = s.query(Strategy).filter_by(name="S007").one_or_none()
    if strat is None:
        strat = Strategy(name="S007", broker="CTRADER")
        s.add(strat)
    s.add(acc)
    s.flush()
    link = AccountStrategy(account_id=acc.id, strategy_id=strat.id, enabled=True, status="idle",
                           risk_pct=risk_pct, fixed_lot=fixed_lot, use_fixed_lot=use_fixed_lot,
                           initial_balance=initial_balance)
    s.add(link)
    s.commit()
    link_id = link.id
    s.close()
    return link_id


def install_fake_by_account(monkeypatch, brokers_by_account_id: dict):
    def _ctor(creds=None, require_account=True):
        return brokers_by_account_id[creds["account_id"]]
    fake_mod = types.SimpleNamespace(CTraderS007=_ctor)
    monkeypatch.setitem(sys.modules, "bot.ctrader_s007", fake_mod)


def _tick(Session, link_id, ts, fake, m1_day):
    fake.m1 = m1_day.loc[:ts]
    s = Session()
    link = s.get(AccountStrategy, link_id)
    rc = runner._worker_s007(link, s, None)
    return rc


def _run_day_via_runner(Session, link_id, fake, m1_day, *, cycle_from="10:00", cycle_to="14:30"):
    time_only = m1_day.index.strftime("%H:%M")
    mask = (time_only >= cycle_from) & (time_only <= cycle_to)
    prev_ts = None
    for ts in m1_day.index[mask]:
        if prev_ts is not None:
            fake.apply_bar(m1_day.loc[ts])
        _tick(Session, link_id, ts, fake, m1_day)
        prev_ts = ts


def _scenario_a_up(date):
    return make_day(date, fr_low=FR_LOW, fr_high=FR_HIGH,
                    close={0: MID - 1, 1: FR_HIGH - 49, 2: FR_HIGH - 48})


def test_c1_first_cycle_opens_a_real_position_row(Session, monkeypatch):
    link_id = _make_link(Session)
    fake = FakeCTraderS007E2E()
    install_fake_by_account(monkeypatch, {1: fake})
    m1 = _scenario_a_up("2026-08-01")

    _run_day_via_runner(Session, link_id, fake, m1, cycle_to="10:05")

    s = Session()
    rows = s.query(Position).all()
    assert len(rows) == 1
    assert rows[0].status == "open"
    assert rows[0].side == "buy"
    s.close()


def test_c2_day_resolution_stamps_settled_status(Session, monkeypatch):
    link_id = _make_link(Session)
    fake = FakeCTraderS007E2E()
    install_fake_by_account(monkeypatch, {1: fake})
    m1 = _scenario_a_up("2026-08-02")
    m1.loc[m1.index[63], "high"] = m1.loc[m1.index[63], "high"] + 300  # touch tp fast

    from tests.e2e.conftest import oracle, resolved_cfg
    truth = oracle(m1, FR_LOW, FR_HIGH, resolved_cfg(None))
    tp = truth["tp"]
    m1.loc[m1.index[63], "high"] = tp + 1
    m1.loc[m1.index[63], "close"] = tp

    _run_day_via_runner(Session, link_id, fake, m1, cycle_to="10:05")

    today = datetime.datetime.now().date().isoformat()
    s = Session()
    link = s.get(AccountStrategy, link_id)
    assert link.status == f"settled:{today}"
    s.close()


def test_c3_later_tick_same_day_skips_the_broker_entirely(Session, monkeypatch):
    link_id = _make_link(Session)
    fake = FakeCTraderS007E2E()
    install_fake_by_account(monkeypatch, {1: fake})
    today = datetime.datetime.now().date().isoformat()

    s = Session()
    link = s.get(AccountStrategy, link_id)
    link.status = f"settled:{today}"
    s.commit()
    s.close()

    cycles_before = len(fake.cycles)
    s2 = Session()
    link2 = s2.get(AccountStrategy, link_id)
    rc = runner._worker_s007(link2, s2, None)
    assert rc == 0
    assert len(fake.cycles) == cycles_before  # no broker session opened at all


def test_c4_stale_settled_date_runs_normally(Session, monkeypatch):
    link_id = _make_link(Session)
    fake = FakeCTraderS007E2E()
    install_fake_by_account(monkeypatch, {1: fake})
    s = Session()
    link = s.get(AccountStrategy, link_id)
    link.status = "settled:2020-01-01"
    s.commit()
    s.close()

    m1 = _scenario_a_up("2026-08-04")
    _run_day_via_runner(Session, link_id, fake, m1, cycle_to="10:05")
    assert len(fake.cycles) > 0


def test_c5_two_accounts_keep_independent_day_caps(Session, monkeypatch):
    """risk_pct sized so max_positions_per_day == 1 for both accounts --
    each account's own opened-today count must come from ITS OWN per-account
    log (S007-acct<id>/), never from the other account's."""
    link_a = _make_link(Session, external_account_id="101", risk_pct=2.0,
                        initial_balance=10_000.0)
    link_b = _make_link(Session, external_account_id="102", risk_pct=2.0,
                        initial_balance=10_000.0)
    fake_a = FakeCTraderS007E2E()
    fake_b = FakeCTraderS007E2E()
    install_fake_by_account(monkeypatch, {101: fake_a, 102: fake_b})

    m1_a = _scenario_a_up("2026-08-05")
    m1_b = _scenario_a_up("2026-08-05")

    time_only = m1_a.index.strftime("%H:%M")
    mask = (time_only >= "10:00") & (time_only <= "10:05")
    prev_ts = None
    for ts in m1_a.index[mask]:
        if prev_ts is not None:
            fake_a.apply_bar(m1_a.loc[ts])
            fake_b.apply_bar(m1_b.loc[ts])
        _tick(Session, link_a, ts, fake_a, m1_a)
        _tick(Session, link_b, ts, fake_b, m1_b)
        prev_ts = ts

    s = Session()
    rows = s.query(Position).all()
    by_account = {}
    for r in rows:
        by_account.setdefault(r.account_id, []).append(r)
    assert len(by_account) == 2
    for acc_positions in by_account.values():
        assert len(acc_positions) == 1  # neither account's count leaked into the other's
    s.close()


def test_c6_position_row_lifecycle_matches_the_broker_ledger(Session, monkeypatch):
    """A server-side stop/tp touch is only reconciled into the Position DB
    row by the SEPARATE sync_positions pass (stubbed out here, per the
    existing test_runner_s007_settle.py convention -- it needs its own
    broker read and is out of scope for this worker-level suite). The
    runner's OWN direct DB write on a `close` action fires only when decide()
    itself emits one -- i.e. an EXIT_END/manual flatten of a position that
    was still genuinely open at the broker, not a server-side stop/tp (which
    is simply gone from `have` by the next reconcile, no explicit action)."""
    link_id = _make_link(Session)
    fake = FakeCTraderS007E2E()
    install_fake_by_account(monkeypatch, {1: fake})
    m1 = make_day("2026-08-06", fr_low=FR_LOW, fr_high=FR_HIGH, n_london=275,
                  close={0: MID - 1, 1: FR_HIGH - 49, 2: FR_HIGH - 48})
    # every later bar stays flat at the default mid -- well inside (stop, tp)

    _run_day_via_runner(Session, link_id, fake, m1, cycle_to="14:30")

    assert fake.positions == {}
    s = Session()
    rows = s.query(Position).all()
    assert len(rows) == 1
    assert rows[0].status == "closed"
    assert rows[0].reason == "flat_time"
    s.close()
