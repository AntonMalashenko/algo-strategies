"""ALGODEV-33: webapp/runner.py::run_coordinator's exit code must reflect a
killed/crashed account worker, not always report 0 -- scripts/
scheduler_tick.py's `_run_item` only ever surfaces a dispatch's diagnostics
(and the process exits with a status Ofelia's own dispatch log records)
when the subprocess's return code is non-zero. Before this fix, a worker
SIGKILLed by the coordinator's own timeout looked identical, in every log
a human actually reads, to a completely clean tick -- the only trace was
the DB's `last_error` (found live 2026-08-28, S011/account_strategy 4).

`subprocess.Popen` is monkeypatched with a fake process object so these
tests never spawn a real `python -m webapp.runner --worker ...` subprocess
-- they exercise run_coordinator's own bookkeeping (return code, DB status/
last_error, which stream the diagnostics land on) given a controlled mix of
"exited 0" / "timed out and got killed" fake workers.
"""
from __future__ import annotations

import os

os.environ.setdefault("APP_SECRET_KEY", "test-only-not-a-real-key")

import subprocess

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import webapp.runner as runner
from webapp.db import Base
from webapp.models import Account, AccountStrategy, Broker, Strategy, User


@pytest.fixture
def engine():
    e = create_engine("sqlite:///:memory:", future=True,
                      poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(e)
    return e


@pytest.fixture
def session_factory(engine, monkeypatch):
    # run_coordinator calls the module-level get_session() itself (not an
    # injected session) -- route it at the real StaticPool engine so all
    # the sessions it opens see the same in-memory DB as the test's own.
    factory = sessionmaker(bind=engine, future=True)
    monkeypatch.setattr(runner, "get_session", factory)
    return factory


@pytest.fixture
def two_links(engine, session_factory):
    s = session_factory()
    u = User(username="t", password_hash="x", is_admin=True)
    broker = Broker(name="IC Markets", platforms="CTRADER")
    s.add_all([u, broker])
    s.flush()
    strat = Strategy(name="S011", broker="CTRADER")
    s.add(strat)
    s.flush()
    links = []
    for i in range(2):
        acc = Account(user_id=u.id, broker="CTRADER", broker_id=broker.id,
                      external_account_id=str(100 + i), env="demo", label=f"acct-{i}")
        s.add(acc)
        s.flush()
        link = AccountStrategy(account_id=acc.id, strategy_id=strat.id, enabled=True)
        s.add(link)
        s.flush()
        links.append(link.id)
    s.commit()
    s.close()
    return links


class _FakeProc:
    """Stands in for subprocess.Popen -- `times_out=True` makes .wait(timeout=...)
    raise TimeoutExpired exactly like a real straggler would, so
    run_coordinator's except-branch (p.kill(); p.wait(); results[lid] = -9)
    runs for real."""

    def __init__(self, times_out: bool):
        self.times_out = times_out
        self.killed = False

    def wait(self, timeout=None):
        if timeout is not None and self.times_out:
            raise subprocess.TimeoutExpired(cmd="webapp.runner --worker", timeout=timeout)
        return 0

    def kill(self):
        self.killed = True


def test_all_workers_ok_returns_zero_and_stays_quiet_on_stdout(
        two_links, session_factory, monkeypatch, capsys):
    monkeypatch.setattr(runner.subprocess, "Popen", lambda *a, **k: _FakeProc(times_out=False))
    rc = runner.run_coordinator("S011", timeout_s=5.0)
    assert rc == 0
    captured = capsys.readouterr()
    assert captured.err == ""                    # nothing diagnostic-worthy happened


def test_one_killed_worker_returns_nonzero_and_writes_stderr(
        two_links, session_factory, monkeypatch, capsys):
    procs = iter([_FakeProc(times_out=True), _FakeProc(times_out=False)])
    monkeypatch.setattr(runner.subprocess, "Popen", lambda *a, **k: next(procs))

    rc = runner.run_coordinator("S011", timeout_s=5.0)

    assert rc == 1                                # ALGODEV-33: no longer hardcoded 0
    captured = capsys.readouterr()
    assert "TIMEOUT after 5s, killed" in captured.err
    assert f"account_strategy {two_links[0]}" in captured.err


def test_killed_worker_marks_db_status_error(two_links, session_factory, monkeypatch):
    monkeypatch.setattr(runner.subprocess, "Popen", lambda *a, **k: _FakeProc(times_out=True))
    runner.run_coordinator("S011", timeout_s=5.0)

    s = session_factory()
    link = s.get(AccountStrategy, two_links[0])
    assert link.status == "error"
    assert "killed/crashed before self-reporting" in link.last_error
    s.close()
