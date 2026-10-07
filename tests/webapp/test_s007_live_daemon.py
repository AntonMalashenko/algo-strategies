"""ALGODEV-45 step 4: the two things that make the S007 live daemon safe.

1. MUTUAL EXCLUSION -- the daemon holds one account's trading lock for its
   whole run, so an Ofelia-dispatched worker firing in the same minute skips
   instead of placing a second, duplicate entry on the same account.
2. THE SYNC/ASYNC SEAM -- a persistent adapter must hand its cycle to the
   daemon's reactor (blockingCallFromThread) instead of opening its own
   session via _run(), otherwise it would try to start a second reactor and
   raise ReactorNotRestartable mid-trading-day.

Both are failure modes that cost real money and neither is visible by
reading one file, hence a test rather than a comment.
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
from webapp.models import Account, AccountStrategy, Broker, Strategy, User


@pytest.fixture(autouse=True)
def _isolate_root(tmp_path, monkeypatch):
    """Keeps both the lock files and _worker_s007's StrategyLogger output out
    of the real data/ and reports/ trees."""
    monkeypatch.setattr(runner, "ROOT", tmp_path)


@pytest.fixture
def session():
    e = create_engine("sqlite:///:memory:", future=True,
                      poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(e)
    s = sessionmaker(bind=e, future=True)()
    yield s
    s.close()


@pytest.fixture
def s007_link(session):
    u = User(username="t", password_hash="x", is_admin=True)
    broker = Broker(name="IC Markets", platforms="CTRADER")
    session.add_all([u, broker])
    session.flush()
    acc = Account(user_id=u.id, broker="CTRADER", broker_id=broker.id,
                  external_account_id="1", env="demo", label="demo1")
    strat = Strategy(name="S007", broker="CTRADER")
    session.add_all([acc, strat])
    session.flush()
    link = AccountStrategy(account_id=acc.id, strategy_id=strat.id, enabled=True,
                           status="idle")
    session.add(link)
    session.commit()
    return link


def _fake_result(**overrides):
    base = dict(cycle_id="c1", actions=[], error=None, day_done=False,
                in_window=True, filtered=False, manual_stop=False)
    base.update(overrides)
    return base


def test_second_holder_of_the_cycle_lock_is_refused():
    with runner.cycle_lock(1) as first:
        assert first is True
        with runner.cycle_lock(1) as second:
            assert second is False


def test_cycle_lock_is_per_account_strategy():
    with runner.cycle_lock(1) as a, runner.cycle_lock(2) as b:
        assert (a, b) == (True, True)


def test_worker_skips_the_cycle_while_the_daemon_holds_the_lock(session, s007_link,
                                                                monkeypatch):
    """The whole point: Ofelia's per-minute tick must NOT trade an account a
    persistent daemon is already trading."""
    calls = []
    monkeypatch.setattr(runner, "run_s007_cycle",
                        lambda *a, **k: calls.append(1) or _fake_result())
    monkeypatch.setattr(runner, "_sync_after_cycle", lambda *a, **k: None)

    with runner.cycle_lock(s007_link.id):          # stands in for the daemon
        rc = runner._worker_s007(s007_link, session, None)

    assert rc == 0
    assert calls == []


def test_worker_with_an_injected_api_does_not_retake_the_lock(session, s007_link,
                                                              monkeypatch):
    """The daemon already holds the lock for its whole run; re-taking it per
    tick would make every one of its own cycles skip itself."""
    seen = []
    monkeypatch.setattr(runner, "run_s007_cycle",
                        lambda *a, **k: seen.append(k.get("api")) or _fake_result())
    monkeypatch.setattr(runner, "_sync_after_cycle", lambda *a, **k: None)
    sentinel = object()

    with runner.cycle_lock(s007_link.id):          # the daemon's own lock
        runner._worker_s007(s007_link, session, None, api=sentinel)

    assert seen == [sentinel]


def test_persistent_adapter_routes_the_cycle_onto_the_daemon_reactor(monkeypatch):
    """A persistent CTraderS007 must never fall through to _run(): that opens
    a second session and restarts the reactor."""
    from bot.ctrader_s007 import CTraderS007
    import twisted.internet.threads as threads

    api = CTraderS007.__new__(CTraderS007)
    api._persistent = True
    api._run = lambda *a, **k: pytest.fail("_run() must not be reached in persistent mode")

    captured = {}

    def fake_blocking(reactor, fn, *args):
        captured["fn"] = fn
        captured["args"] = args
        return "cycle-result"
    monkeypatch.setattr(threads, "blockingCallFromThread", fake_blocking)

    decide = object()
    out = api.run_live_cycle(["DE40"], 7, decide)

    assert out == "cycle-result"
    assert captured["fn"] == api.live_tick_step
    assert captured["args"] == (["DE40"], 7, decide)


def test_non_persistent_adapter_still_opens_its_own_session():
    """The unchanged Ofelia path: no daemon, so the adapter runs its own
    connect/auth/disconnect exactly as before this ticket."""
    from bot.ctrader_s007 import CTraderS007

    api = CTraderS007.__new__(CTraderS007)
    seen = []
    api._run = lambda work: seen.append(work) or "ran"

    assert api.run_live_cycle(["DE40"], 7, object()) == "ran"
    assert len(seen) == 1


def test_settle_marker_still_short_circuits_under_the_lock(session, s007_link,
                                                           monkeypatch):
    """Guards the ordering: the lock wraps the cycle, so the day-done
    short-circuit (which must not even build credentials) still runs."""
    s007_link.status = f"settled:{datetime.datetime.now().date().isoformat()}"
    session.commit()
    calls = []
    monkeypatch.setattr(runner, "run_s007_cycle",
                        lambda *a, **k: calls.append(1) or _fake_result())

    assert runner._worker_s007(s007_link, session, None) == 0
    assert calls == []


def test_post_cycle_sync_reuses_the_open_session_instead_of_spawning(monkeypatch,
                                                                     session, s007_link):
    """The subprocess only ever existed because a one-shot worker could not
    run a second reactor. Keeping it under the daemon would pay a fresh
    handshake every minute -- the exact cost this ticket removes."""
    import webapp.sync_positions as sync

    spawned, in_process = [], []
    monkeypatch.setattr(sync, "spawn_sync_worker", lambda *a, **k: spawned.append(a))
    monkeypatch.setattr(sync, "sync_account_strategy",
                        lambda s, link, **k: in_process.append(k.get("api")))
    monkeypatch.setattr(runner, "get_session", lambda: session)
    sentinel = object()

    runner._sync_after_cycle(s007_link.id, 0.0, None, sentinel)

    assert spawned == []
    assert in_process == [sentinel]


def test_post_cycle_sync_without_a_session_still_spawns_the_subprocess(monkeypatch):
    """The unchanged Ofelia path."""
    import webapp.sync_positions as sync

    spawned = []
    monkeypatch.setattr(sync, "spawn_sync_worker", lambda *a, **k: spawned.append(a))

    runner._sync_after_cycle(7, 0.0, None)

    assert len(spawned) == 1
