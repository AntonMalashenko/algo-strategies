"""webapp/ctrader_tokens.py -- the central, locked cTrader token refresh
every DB-driven path (runner workers, position sync, position audit, S007
daemon) goes through (ALGODEV-48).

What it defends: S007 and S021 trade the SAME cTrader account from separate
worker processes that tick in the same minute. Without a lock, both would
refresh with the same refresh token when it crosses the threshold; the broker
may rotate it, so the loser persists a pair that is already dead and every
later cycle fails with CH_ACCESS_TOKEN_INVALID.
"""
from __future__ import annotations

import fcntl
import multiprocessing
import os
from datetime import datetime, timedelta, timezone

os.environ.setdefault("APP_SECRET_KEY", "test-only-not-a-real-key")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import webapp.ctrader_tokens as tokens
from bot.clients.ctrader import auth
from webapp.db import Base
from webapp.models import Account, Broker, User

FAR_FUTURE = (datetime.now(timezone.utc) + timedelta(days=20)).isoformat()
ALREADY_EXPIRED = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
RENEWED_EXPIRY = datetime.now(timezone.utc) + timedelta(days=30)


def _make_account(db, *, expires_at=ALREADY_EXPIRED, refresh_token="old-refresh"):
    user = User(username="t", password_hash="x", is_admin=True)
    broker = Broker(name="IC Markets", platforms="CTRADER")
    db.add_all([user, broker])
    db.flush()
    acc = Account(user_id=user.id, broker="CTRADER", broker_id=broker.id,
                  external_account_id="47939312", env="demo", label="ctrader-x",
                  broker_host="demo.ctraderapi.com")
    acc.credentials = {"client_id": "cid", "client_secret": "sec",
                       "access_token": "old-access", "refresh_token": refresh_token,
                       "token_expires_at": expires_at}
    db.add(acc)
    db.commit()
    return acc


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:", future=True, poolclass=StaticPool,
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, future=True)()
    yield db
    db.close()


@pytest.fixture(autouse=True)
def lock_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(tokens, "TOKEN_LOCK_DIR", tmp_path / "locks")
    return tmp_path / "locks"


@pytest.fixture
def refresh_calls(monkeypatch):
    calls = []

    def fake_refresh(client_id, client_secret, refresh_token):
        calls.append(refresh_token)
        return auth.TokenBundle(access_token="new-access", refresh_token="rotated-refresh",
                                expires_at=RENEWED_EXPIRY)
    monkeypatch.setattr(auth, "refresh_access_token", fake_refresh)
    return calls


def _stored(db, acc):
    db.expire_all()
    return db.get(Account, acc.id).credentials


class TestFreshCtraderCreds:
    def test_valid_token_is_returned_untouched(self, session, refresh_calls):
        acc = _make_account(session, expires_at=FAR_FUTURE)
        creds = tokens.fresh_ctrader_creds(session, acc)
        assert refresh_calls == []
        assert creds["access_token"] == "old-access"
        assert creds["account_id"] == 47939312 and creds["host"] == "demo.ctraderapi.com"

    def test_expired_token_is_renewed_persisted_and_returned(self, session, refresh_calls):
        acc = _make_account(session)
        creds = tokens.fresh_ctrader_creds(session, acc)

        assert refresh_calls == ["old-refresh"]
        assert creds["access_token"] == "new-access"
        assert creds["refresh_token"] == "rotated-refresh"
        stored = _stored(session, acc)
        assert stored["access_token"] == "new-access"
        assert stored["refresh_token"] == "rotated-refresh"
        assert stored["token_expires_at"] == RENEWED_EXPIRY.isoformat()
        assert stored["client_secret"] == "sec"          # merged, not replaced

    def test_central_leeway_is_wider_than_the_session_preflight(self):
        """If the central refresh says "still valid", the adapter's own
        pre-flight a few seconds later must agree -- otherwise it would
        renew OUTSIDE the lock."""
        assert tokens.CENTRAL_REFRESH_LEEWAY_S > auth.REFRESH_LEEWAY_S

    def test_token_inside_the_central_leeway_is_renewed_early(self, session, refresh_calls):
        ninety_min = (datetime.now(timezone.utc) + timedelta(minutes=90)).isoformat()
        acc = _make_account(session, expires_at=ninety_min)
        tokens.fresh_ctrader_creds(session, acc)
        assert refresh_calls == ["old-refresh"]

    def test_no_refresh_token_skips_the_lock_and_the_refresh(self, session, refresh_calls,
                                                            lock_dir):
        acc = _make_account(session, refresh_token=None)
        creds = tokens.fresh_ctrader_creds(session, acc)
        assert refresh_calls == [] and creds["access_token"] == "old-access"
        assert not lock_dir.exists()

    def test_a_refresh_done_by_another_process_is_picked_up_not_repeated(
            self, session, refresh_calls):
        """The in-memory account says "expired", but by the time we hold the
        lock the DB already has another worker's renewed pair."""
        acc = _make_account(session)
        other = sessionmaker(bind=session.get_bind(), future=True)()
        other_acc = other.get(Account, acc.id)
        other_acc.credentials = {**other_acc.credentials, "access_token": "their-access",
                                 "refresh_token": "their-refresh",
                                 "token_expires_at": FAR_FUTURE}
        other.commit()
        other.close()

        creds = tokens.fresh_ctrader_creds(session, acc)

        assert refresh_calls == []
        assert creds["access_token"] == "their-access"
        assert creds["refresh_token"] == "their-refresh"

    def test_refused_refresh_degrades_to_the_stored_creds(self, session, monkeypatch):
        def refuse(*_args):
            raise auth.TokenRefreshError(f"{auth.TOKEN_REQUEST_FAILED_PREFIX} (HTTP 400)")
        monkeypatch.setattr(auth, "refresh_access_token", refuse)
        acc = _make_account(session)
        creds = tokens.fresh_ctrader_creds(session, acc)
        assert creds["access_token"] == "old-access"
        assert _stored(session, acc)["refresh_token"] == "old-refresh"

    def test_busy_lock_proceeds_without_refreshing(self, session, refresh_calls,
                                                   lock_dir, monkeypatch):
        monkeypatch.setattr(tokens, "LOCK_WAIT_S", 0.3)
        acc = _make_account(session)
        lock_dir.mkdir(parents=True, exist_ok=True)
        with open(lock_dir / f"account-{acc.id}.lock", "a+") as holder:
            fcntl.flock(holder, fcntl.LOCK_EX)
            creds = tokens.fresh_ctrader_creds(session, acc)
        assert refresh_calls == []
        assert creds["access_token"] == "old-access"


def _worker(db_url, account_id, lock_dir, calls_file, barrier):
    """One 'worker process': refreshes through the real lock against a shared
    sqlite file. The fake token endpoint appends to `calls_file` so the parent
    can count renewals across processes."""
    import time as _time
    engine = create_engine(db_url, future=True)
    db = sessionmaker(bind=engine, future=True)()
    tokens.TOKEN_LOCK_DIR = lock_dir

    def slow_refresh(client_id, client_secret, refresh_token):
        with open(calls_file, "a") as fh:
            fh.write(refresh_token + "\n")
        _time.sleep(0.5)      # hold the lock long enough for the other to queue
        return auth.TokenBundle(access_token="new-access", refresh_token="rotated-refresh",
                                expires_at=RENEWED_EXPIRY)
    auth.refresh_access_token = slow_refresh
    acc = db.get(Account, account_id)
    barrier.wait()
    tokens.fresh_ctrader_creds(db, acc)
    db.close()


def test_two_processes_on_one_account_refresh_exactly_once(tmp_path, lock_dir):
    """The real race: S007 and S021 workers start on the same account in the
    same minute, both seeing an expired token."""
    db_url = f"sqlite:///{tmp_path / 'app.db'}"
    engine = create_engine(db_url, future=True)
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, future=True)()
    acc = _make_account(db)
    account_id = acc.id
    db.close()

    ctx = multiprocessing.get_context("fork")
    barrier = ctx.Barrier(2)
    calls_file = tmp_path / "calls.txt"
    procs = [ctx.Process(target=_worker,
                         args=(db_url, account_id, lock_dir, calls_file, barrier))
             for _ in range(2)]
    for proc in procs:
        proc.start()
    for proc in procs:
        proc.join(timeout=30)
        assert proc.exitcode == 0

    assert calls_file.read_text().splitlines() == ["old-refresh"]
    db = sessionmaker(bind=engine, future=True)()
    stored = db.get(Account, account_id).credentials
    assert stored["refresh_token"] == "rotated-refresh"
    db.close()
