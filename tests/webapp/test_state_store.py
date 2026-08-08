"""webapp/state_store.py::DBStateStore -- the DB-backed counterpart to
utils/strategy_state.py::FileStateStore, one row per account_strategy_id in
the `strategy_state` table (webapp/models.py::StrategyState).

Uses a private in-memory engine bound to the real webapp.models.Base
metadata (not webapp.db's process-wide singleton engine, which is bound to
whatever APP_DB_URL was at first import across the whole test session) --
fully isolated, no risk to the real data/app.db, no import-order fragility.
"""
from __future__ import annotations

import os

os.environ.setdefault("APP_SECRET_KEY", "test-only-not-a-real-key")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from webapp.db import Base
from webapp.models import Account, AccountStrategy, Strategy, User
from webapp.state_store import DBStateStore


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine, future=True)()
    yield s
    s.close()


@pytest.fixture
def two_links(session):
    u = User(username="t", password_hash="x", is_admin=True)
    session.add(u)
    session.flush()
    acc_a = Account(user_id=u.id, broker="BYBIT", external_account_id="a", env="mainnet", label="a")
    acc_b = Account(user_id=u.id, broker="BYBIT", external_account_id="b", env="mainnet", label="b")
    strat = Strategy(name="S009", broker="BYBIT")
    session.add_all([acc_a, acc_b, strat])
    session.flush()
    link_a = AccountStrategy(account_id=acc_a.id, strategy_id=strat.id, enabled=True)
    link_b = AccountStrategy(account_id=acc_b.id, strategy_id=strat.id, enabled=True)
    session.add_all([link_a, link_b])
    session.commit()
    return link_a, link_b


def test_load_with_no_row_yet_returns_empty_dict(session, two_links):
    link_a, _ = two_links
    assert DBStateStore(link_a.id, session).load() == {}


def test_save_then_load_round_trips(session, two_links):
    link_a, _ = two_links
    store = DBStateStore(link_a.id, session)
    store.save({"last_day": 100, "equity": 1.05, "book": {"BTCUSDT": 0.3}})
    assert DBStateStore(link_a.id, session).load() == {"last_day": 100, "equity": 1.05,
                                                        "book": {"BTCUSDT": 0.3}}


def test_second_save_updates_the_same_row_not_a_duplicate(session, two_links):
    link_a, _ = two_links
    store = DBStateStore(link_a.id, session)
    store.save({"last_day": 100, "equity": 1.0, "book": {}})
    store.save({"last_day": 101, "equity": 1.02, "book": {"ETHUSDT": -0.2}})

    from webapp.models import StrategyState
    rows = session.query(StrategyState).filter_by(account_strategy_id=link_a.id).all()
    assert len(rows) == 1
    assert store.load()["last_day"] == 101


def test_two_accounts_do_not_collide(session, two_links):
    link_a, link_b = two_links
    DBStateStore(link_a.id, session).save({"last_day": 1, "equity": 1.0, "book": {"A": 1}})
    DBStateStore(link_b.id, session).save({"last_day": 2, "equity": 2.0, "book": {"B": 1}})

    assert DBStateStore(link_a.id, session).load()["book"] == {"A": 1}
    assert DBStateStore(link_b.id, session).load()["book"] == {"B": 1}
