"""webapp/runner.py::_worker_s007's day-done short-circuit.

Regression coverage for a real cost bug found 2026-08-08 while building the
Docker/Ofelia scaffold: unlike the legacy scripts/s007_tick.py (which has a
loop_settled/loop_resumed guard), the DB-driven S007 worker used to open a
real cTrader/Twisted session on every dispatcher tick for the rest of the
session window, even after the day's target was already reached. Fixed by
stamping `link.status = "settled:<local date>"` once a cycle comes back
day_done/filtered/manual_stop, and short-circuiting before ANY broker work
(not even constructing StrategyLogger/creds) on a later tick that still
matches today's date.

run_s007_cycle is monkeypatched at the webapp.runner module level (it's a
top-level `from bot.s007_paper import run_cycle_for_account as
run_s007_cycle`, unlike S009's local-import pattern) so no real cTrader
session is ever opened here.
"""
from __future__ import annotations

import datetime
import os

os.environ.setdefault("APP_SECRET_KEY", "test-only-not-a-real-key")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import webapp.runner as runner
from webapp.db import Base
from webapp.models import Account, AccountStrategy, Strategy, User


@pytest.fixture(autouse=True)
def _isolate_logs(tmp_path, monkeypatch):
    """_worker_s007 builds StrategyLogger(..., log_root=str(ROOT / "reports"
    / "logs")) -- without this, every non-short-circuited test in this file
    would write real files into reports/logs/S007-acct1/. Same class of leak
    as tests/bot/test_s007_live_sizing.py's _clean_position_log fixture and
    tests/bot/test_s009_paper.py's _isolate_paper_dirs."""
    monkeypatch.setattr(runner, "ROOT", tmp_path)


@pytest.fixture
def engine():
    e = create_engine("sqlite:///:memory:", future=True,
                      poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(e)
    return e


@pytest.fixture
def session(engine):
    s = sessionmaker(bind=engine, future=True)()
    yield s
    s.close()


@pytest.fixture
def s007_link(session):
    u = User(username="t", password_hash="x", is_admin=True)
    session.add(u)
    session.flush()
    acc = Account(user_id=u.id, broker="CTRADER", external_account_id="1",
                 env="demo", label="demo1")
    strat = Strategy(name="S007", broker="CTRADER")
    session.add_all([acc, strat])
    session.flush()
    link = AccountStrategy(account_id=acc.id, strategy_id=strat.id, enabled=True, status="idle")
    session.add(link)
    session.commit()
    return link


def _fake_result(**overrides):
    base = dict(cycle_id="c1", actions=[], error=None, day_done=False,
               in_window=True, filtered=False, manual_stop=False)
    base.update(overrides)
    return base


def test_day_done_cycle_stamps_settled_status(engine, session, s007_link, monkeypatch):
    link_id = s007_link.id
    monkeypatch.setattr(runner, "run_s007_cycle", lambda *a, **k: _fake_result(day_done=True))
    monkeypatch.setattr(runner, "_sync_after_cycle", lambda *a, **k: None)

    runner._worker_s007(s007_link, session, None)

    today = datetime.datetime.now().date().isoformat()
    check = sessionmaker(bind=engine, future=True)()
    reloaded = check.get(AccountStrategy, link_id)
    assert reloaded.status == f"settled:{today}"
    check.close()


def test_second_tick_same_day_skips_broker_entirely(session, s007_link, monkeypatch):
    today = datetime.datetime.now().date().isoformat()
    s007_link.status = f"settled:{today}"
    session.commit()

    calls = []
    monkeypatch.setattr(runner, "run_s007_cycle", lambda *a, **k: calls.append(1) or _fake_result())

    rc = runner._worker_s007(s007_link, session, None)

    assert rc == 0
    assert calls == []   # run_s007_cycle (and therefore CTraderS007) never invoked


def test_stale_settled_date_runs_normally(session, s007_link, monkeypatch):
    s007_link.status = "settled:2020-01-01"   # a date that is never today
    session.commit()

    calls = []
    monkeypatch.setattr(runner, "run_s007_cycle", lambda *a, **k: calls.append(1) or _fake_result())
    monkeypatch.setattr(runner, "_sync_after_cycle", lambda *a, **k: None)

    runner._worker_s007(s007_link, session, None)

    assert calls == [1]


def test_non_settled_cycle_keeps_the_live_idle_status_format(engine, session, s007_link, monkeypatch):
    link_id = s007_link.id
    monkeypatch.setattr(runner, "run_s007_cycle", lambda *a, **k: _fake_result(day_done=False))
    monkeypatch.setattr(runner, "_sync_after_cycle", lambda *a, **k: None)

    runner._worker_s007(s007_link, session, None)

    check = sessionmaker(bind=engine, future=True)()
    reloaded = check.get(AccountStrategy, link_id)
    assert reloaded.status == "idle"   # unchanged from before this fix, no actions
    check.close()
