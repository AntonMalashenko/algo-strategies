"""ALGODEV-31: bot/symbol_resolver.py::resolve_symbol -- the three outcomes
(verified-row hit / fallback hit / hard failure) plus a regression check
that the already-live S007/IC Markets account resolves to the exact same
symbol it always has, so the switch away from guessing SYMBOL_CANDIDATES
cannot silently move a funded account's orders to the wrong instrument.
"""
from __future__ import annotations

import os

os.environ.setdefault("APP_SECRET_KEY", "test-only-not-a-real-key")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from bot import s007_config as C
from bot.symbol_resolver import resolve_symbol
from webapp.db import Base
from webapp.models import Asset, Broker, BrokerAssetSymbol


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:", future=True,
                           poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine, future=True)()
    yield s
    s.close()


@pytest.fixture
def broker_and_asset(session):
    broker = Broker(name="IC Markets", platforms="CTRADER")
    asset = Asset(symbol="GER40", asset_class="index_cfd")
    session.add_all([broker, asset])
    session.commit()
    return broker, asset


def test_verified_row_hit_returns_single_confirmed_symbol(session, broker_and_asset):
    broker, asset = broker_and_asset
    session.add(BrokerAssetSymbol(broker_id=broker.id, asset_id=asset.id,
                                  platform="CTRADER", broker_symbol="DE40"))
    session.commit()
    result = resolve_symbol(session=session, broker_id=broker.id, asset_symbol="GER40",
                            platform="CTRADER", fallback_candidates=["GER40", "DE40"])
    assert result == ["DE40"]


def test_no_row_falls_back_to_candidate_list_unchanged(session, broker_and_asset, caplog):
    broker, asset = broker_and_asset
    fallback = ["GER40", "DE40", "GERMANY40"]
    result = resolve_symbol(session=session, broker_id=broker.id, asset_symbol="GER40",
                            platform="CTRADER", fallback_candidates=fallback)
    assert result == fallback
    assert result is not fallback  # returns a copy, not the caller's own list object
    assert any(r.levelname == "WARNING" for r in caplog.records)


def test_unknown_asset_symbol_also_falls_back(session, broker_and_asset):
    """No Asset row at all for the given symbol (typo, or an asset never
    seeded) must behave exactly like "no broker_asset_symbols row" -- not a
    different code path, not a crash on a None asset id."""
    broker, _asset = broker_and_asset
    result = resolve_symbol(session=session, broker_id=broker.id, asset_symbol="NOPE",
                            platform="CTRADER", fallback_candidates=["X"])
    assert result == ["X"]


def test_no_row_and_no_fallback_is_a_hard_failure(session, broker_and_asset):
    broker, _asset = broker_and_asset
    with pytest.raises(SystemExit):
        resolve_symbol(session=session, broker_id=broker.id, asset_symbol="GER40",
                       platform="CTRADER", fallback_candidates=None)


def test_no_row_and_empty_fallback_list_is_also_a_hard_failure(session, broker_and_asset):
    broker, _asset = broker_and_asset
    with pytest.raises(SystemExit):
        resolve_symbol(session=session, broker_id=broker.id, asset_symbol="GER40",
                       platform="CTRADER", fallback_candidates=[])


def test_wrong_broker_id_does_not_match_another_brokers_verified_row(session, broker_and_asset):
    """A verified row is scoped to ONE broker -- a different broker_id must
    not accidentally inherit it, even for the same asset+platform string."""
    broker, asset = broker_and_asset
    other_broker = Broker(name="Some Other Broker", platforms="CTRADER")
    session.add(other_broker)
    session.commit()
    session.add(BrokerAssetSymbol(broker_id=broker.id, asset_id=asset.id,
                                  platform="CTRADER", broker_symbol="DE40"))
    session.commit()
    result = resolve_symbol(session=session, broker_id=other_broker.id, asset_symbol="GER40",
                            platform="CTRADER", fallback_candidates=["GER40-FALLBACK"])
    assert result == ["GER40-FALLBACK"]


def test_regression_live_ic_markets_account_resolves_to_the_same_symbol_as_before(
        session, broker_and_asset):
    """Byte-for-byte regression: before ALGODEV-31, the live S007/IC Markets
    account (currently GER40) traded whatever bot/ctrader_s007.py's live
    candidate-matching first found present in C.SYMBOL_CANDIDATES against
    the broker's real symbol list -- confirmed live 2026-08-25 to be "DE40"
    (see reports/logs/S007-acct47939312/events-2026-08-25.jsonl and the
    verified broker_asset_symbols row added via webapp.cli verify-symbol).
    After the switch, resolve_symbol() must return that exact same symbol,
    as the resolver's only candidate, so this account's orders keep landing
    on the identical instrument."""
    broker, asset = broker_and_asset
    live_verified_symbol = "DE40"
    assert live_verified_symbol in C.SYMBOL_CANDIDATES, (
        "sanity check: the verified symbol must have been a reachable candidate "
        "under the OLD guess-list logic too, or this isn't actually the same instrument")
    session.add(BrokerAssetSymbol(broker_id=broker.id, asset_id=asset.id,
                                  platform=C.PLATFORM, broker_symbol=live_verified_symbol))
    session.commit()

    result = resolve_symbol(session=session, broker_id=broker.id, asset_symbol=C.ASSET_SYMBOL,
                            platform=C.PLATFORM, fallback_candidates=C.SYMBOL_CANDIDATES)

    assert result == [live_verified_symbol]
