"""webapp/runner.py's cTrader credential plumbing.

Two things this defends, both introduced with OAuth2 auto-refresh:

  1. `_ctrader_creds` is the ONE place a cTrader credential dict is built, so
     the refresh material reaches every strategy's worker rather than only
     whichever one was remembered when the field was added.
  2. `_token_persister` actually writes a renewed token back to the account's
     encrypted credentials. Without it a refresh is forgotten at the end of
     the cycle, and once the broker rotates the refresh token the next cycle
     would renew with a dead one -- exactly the CH_ACCESS_TOKEN_INVALID
     outage that stopped S011 for 130 consecutive cycles on 2026-09-21..22.
"""
from __future__ import annotations

import os

os.environ.setdefault("APP_SECRET_KEY", "test-only-not-a-real-key")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import webapp.runner as runner
from webapp.db import Base
from webapp.models import Account, Broker, User


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:", future=True, poolclass=StaticPool,
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, future=True)()
    yield db
    db.close()


@pytest.fixture
def account(session):
    user = User(username="t", password_hash="x", is_admin=True)
    broker = Broker(name="IC Markets", platforms="CTRADER")
    session.add_all([user, broker])
    session.flush()
    acc = Account(user_id=user.id, broker="CTRADER", broker_id=broker.id,
                  external_account_id="48354548", env="demo", label="ctrader-x",
                  broker_host="demo.ctraderapi.com")
    acc.credentials = {"client_id": "cid", "client_secret": "sec",
                       "access_token": "old-access", "refresh_token": "old-refresh"}
    session.add(acc)
    session.commit()
    return acc


class TestCtraderCreds:
    def test_it_carries_the_refresh_material(self, account):
        creds = runner._ctrader_creds(account)
        assert creds["refresh_token"] == "old-refresh"
        assert "token_expires_at" in creds

    def test_it_keeps_the_existing_connection_fields(self, account):
        creds = runner._ctrader_creds(account)
        assert creds["client_id"] == "cid"
        assert creds["client_secret"] == "sec"
        assert creds["access_token"] == "old-access"
        assert creds["account_id"] == 48354548     # int, not the string column
        assert creds["host"] == "demo.ctraderapi.com"

    def test_a_missing_external_account_id_is_none_not_zero(self, session, account):
        """`account_id=0` would be sent to the broker as a real account id."""
        account.external_account_id = None
        session.commit()
        assert runner._ctrader_creds(account)["account_id"] is None


class TestTokenPersister:
    def test_a_refresh_is_written_back_and_committed(self, session, account):
        runner._token_persister(session, account)({
            "access_token": "new-access",
            "refresh_token": "rotated-refresh",
            "token_expires_at": "2026-09-26T12:00:00+00:00",
        })

        session.expire_all()
        stored = session.get(Account, account.id).credentials
        assert stored["access_token"] == "new-access"
        assert stored["refresh_token"] == "rotated-refresh"
        assert stored["token_expires_at"] == "2026-09-26T12:00:00+00:00"

    def test_untouched_credential_fields_survive(self, session, account):
        """A partial update must merge, not replace -- dropping client_secret
        here would break every later cycle."""
        runner._token_persister(session, account)({"access_token": "new-access"})

        session.expire_all()
        stored = session.get(Account, account.id).credentials
        assert stored["client_id"] == "cid"
        assert stored["client_secret"] == "sec"
        assert stored["refresh_token"] == "old-refresh"

    def test_the_new_creds_are_readable_by_the_creds_builder(self, session, account):
        """End to end: what the persister writes is what the next cycle reads."""
        runner._token_persister(session, account)({
            "access_token": "new-access", "refresh_token": "rotated-refresh",
            "token_expires_at": "2026-09-26T12:00:00+00:00"})

        session.expire_all()
        creds = runner._ctrader_creds(session.get(Account, account.id))
        assert creds["access_token"] == "new-access"
        assert creds["refresh_token"] == "rotated-refresh"
        assert creds["token_expires_at"] == "2026-09-26T12:00:00+00:00"
