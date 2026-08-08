"""webapp/runner.py's S009 dispatch: STRATEGY_WORKERS registry + _worker_s009.

bot.s009_paper.run_cycle_for_account is monkeypatched (module-level, since
_worker_s009 imports it locally at call time -- see webapp/runner.py's
`from bot.s009_paper import run_cycle_for_account as run_s009_cycle` inside
the function) so these tests exercise the DB plumbing (creds building,
status/last_cycle_at updates, dispatch table) without touching Bybit or the
funding-carry engine. The one thing every test here defends: broker="off",
allow_mainnet=False must ALWAYS be what reaches run_cycle_for_account
through this DB-driven path -- enabling real orders here is a separate,
explicit decision, not a side effect of this refactor.
"""
from __future__ import annotations

import os

os.environ.setdefault("APP_SECRET_KEY", "test-only-not-a-real-key")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import bot.s009_paper as s009_module
import webapp.runner as runner
from webapp.db import Base
from webapp.models import Account, AccountStrategy, Strategy, User


@pytest.fixture
def engine():
    # StaticPool: _worker_s009 closes the session it's given (matches the
    # real worker's contract -- one process, one session, closed when done);
    # a plain pool would hand a fresh connection to a NEW session and see an
    # empty database, since sqlite's `:memory:` is otherwise per-connection.
    # StaticPool keeps ONE connection alive for the whole engine so a
    # post-call session opened to re-check DB state sees the same data.
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
def s009_link(session):
    u = User(username="t", password_hash="x", is_admin=True)
    session.add(u)
    session.flush()
    acc = Account(user_id=u.id, broker="BYBIT", external_account_id="1", env="mainnet", label="Bybit-x")
    acc.credentials = {"api_key": "k", "api_secret": "s"}
    strat = Strategy(name="S009", broker="BYBIT")
    session.add_all([acc, strat])
    session.flush()
    link = AccountStrategy(account_id=acc.id, strategy_id=strat.id, enabled=True)
    session.add(link)
    session.commit()
    return link


@pytest.fixture
def fake_run_cycle(monkeypatch):
    calls = []

    def fake(**kwargs):
        calls.append(kwargs)
        return dict(booked=1, target={"BTCUSDT": 0.3}, equity=1.01, broker_orders=0, error=None,
                   date="2026-08-10", latest_net_ret=0.01, broker_env=None,
                   broker_equity=None, broker_plan=[])

    monkeypatch.setattr(s009_module, "run_cycle_for_account", fake)
    fake.calls = calls
    return fake


def test_worker_s009_always_forces_shadow_only(session, s009_link, fake_run_cycle):
    runner._worker_s009(s009_link, session, None)
    assert len(fake_run_cycle.calls) == 1
    assert fake_run_cycle.calls[0]["broker"] == "off"
    assert fake_run_cycle.calls[0]["allow_mainnet"] is False


def test_worker_s009_builds_creds_from_account_credentials(session, s009_link, fake_run_cycle):
    runner._worker_s009(s009_link, session, None)
    assert fake_run_cycle.calls[0]["creds"] == {"api_key": "k", "api_secret": "s"}


def test_worker_s009_updates_status_and_last_cycle_at_on_success(engine, session, s009_link, fake_run_cycle):
    link_id = s009_link.id
    assert s009_link.last_cycle_at is None
    rc = runner._worker_s009(s009_link, session, None)
    assert rc == 0

    # _worker_s009 closes `session` (matches the real worker's contract) --
    # re-check via a fresh session bound to the same (StaticPool) engine.
    check = sessionmaker(bind=engine, future=True)()
    reloaded = check.get(AccountStrategy, link_id)
    assert "booked" in reloaded.status
    assert reloaded.last_error is None
    assert reloaded.last_cycle_at is not None
    check.close()


def test_worker_s009_records_error_and_nonzero_exit(engine, session, s009_link, monkeypatch):
    link_id = s009_link.id
    monkeypatch.setattr(s009_module, "run_cycle_for_account", lambda **kw: dict(
        booked=0, target={}, equity=None, broker_orders=0, error="boom",
        date=None, latest_net_ret=None, broker_env=None, broker_equity=None, broker_plan=[]))
    rc = runner._worker_s009(s009_link, session, None)
    assert rc == 1

    check = sessionmaker(bind=engine, future=True)()
    reloaded = check.get(AccountStrategy, link_id)
    assert reloaded.status == "error"
    assert reloaded.last_error == "boom"
    check.close()


def test_worker_s009_rejects_non_bybit_account(session, s009_link):
    s009_link.account.broker = "CTRADER"
    with pytest.raises(SystemExit):
        runner._worker_s009(s009_link, session, None)


# --- dispatch table --------------------------------------------------------

def test_strategy_workers_registers_both_strategies():
    assert set(runner.STRATEGY_WORKERS) == {"S007", "S009"}
    assert runner.STRATEGY_WORKERS["S009"] is runner._worker_s009
    assert runner.STRATEGY_WORKERS["S007"] is runner._worker_s007


def test_run_worker_dispatches_s009_by_strategy_name(session, s009_link, fake_run_cycle, monkeypatch):
    monkeypatch.setattr(runner, "get_session", lambda: session)
    rc = runner.run_worker(s009_link.id)
    assert rc == 0
    assert len(fake_run_cycle.calls) == 1


def test_run_worker_raises_for_unregistered_strategy(session, monkeypatch):
    u = User(username="t2", password_hash="x", is_admin=True)
    session.add(u)
    session.flush()
    acc = Account(user_id=u.id, broker="IBKR", external_account_id="2", env="live", label="ibkr")
    strat = Strategy(name="S099", broker="IBKR")
    session.add_all([acc, strat])
    session.flush()
    link = AccountStrategy(account_id=acc.id, strategy_id=strat.id, enabled=True)
    session.add(link)
    session.commit()

    monkeypatch.setattr(runner, "get_session", lambda: session)
    with pytest.raises(SystemExit):
        runner.run_worker(link.id)


def test_run_worker_skips_disabled_link(session, s009_link, fake_run_cycle, monkeypatch):
    s009_link.enabled = False
    session.commit()
    monkeypatch.setattr(runner, "get_session", lambda: session)
    rc = runner.run_worker(s009_link.id)
    assert rc == 0
    assert fake_run_cycle.calls == []
