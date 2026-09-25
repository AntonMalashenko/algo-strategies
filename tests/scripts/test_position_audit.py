"""scripts/position_audit.py -- pre-session stale-position closer for S007/S021.

Found live 2026-09-24: an S021 position survived past its own session close
because the LAST cron cycle of the day missed the 15:59 fixed-EST time-exit
by 2 minutes, and the next day's decide() can never recognize a prior day's
label again (see that script's module docstring). These tests never touch a
real broker/DB -- webapp.db.get_session and the CTrader adapter classes are
monkeypatched throughout.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import scripts.position_audit as pa  # noqa: E402


def _fake_link(strategy_name, account_id=1, external_account_id="47939312"):
    account = SimpleNamespace(
        credentials={"client_id": "id", "client_secret": "secret",
                     "access_token": "token"},
        external_account_id=external_account_id, broker_host=None,
        label=f"ctrader-{external_account_id}",
    )
    strategy = SimpleNamespace(name=strategy_name)
    return SimpleNamespace(strategy=strategy, account=account)


class _FakeSession:
    def __init__(self, link):
        self._link = link

    def get(self, model, id_):
        return self._link

    def close(self):
        pass


def _patch_db(monkeypatch, link):
    fake_db = SimpleNamespace(get_session=lambda: _FakeSession(link))
    monkeypatch.setitem(sys.modules, "webapp.db", fake_db)
    fake_models = SimpleNamespace(AccountStrategy=object)
    monkeypatch.setitem(sys.modules, "webapp.models", fake_models)


class _FakeClient:
    def __init__(self, positions, creds=None):
        self._positions = positions
        self.closed = []

    def open_positions(self):
        return self._positions

    def close_position(self, position_id, volume):
        self.closed.append((position_id, volume))
        return {"ok": True}


def _patch_adapter(monkeypatch, module_path, class_name, client):
    fake_module = SimpleNamespace(**{class_name: lambda creds: client})
    monkeypatch.setitem(sys.modules, module_path, fake_module)


def test_rejects_multi_day_strategies(monkeypatch, tmp_path):
    link = _fake_link("S009")
    _patch_db(monkeypatch, link)
    try:
        pa.audit_account_strategy(2)
        assert False, "expected SystemExit"
    except SystemExit as e:
        assert "S009" in str(e)
        assert "S009/S011 are" in str(e) or "S007/S021" in str(e)


def test_clean_state_closes_nothing(monkeypatch, tmp_path):
    link = _fake_link("S021")
    _patch_db(monkeypatch, link)
    client = _FakeClient(positions=[])
    _patch_adapter(monkeypatch, "bot.ctrader_orb", "CTraderORB", client)
    monkeypatch.setattr(pa, "StrategyLogger",
                        lambda *a, **k: _NullLogger())

    n = pa.audit_account_strategy(5)
    assert n == 0
    assert client.closed == []


def test_stale_position_gets_closed(monkeypatch, tmp_path):
    link = _fake_link("S021")
    _patch_db(monkeypatch, link)
    stale = [{"position_id": 675236786, "label": "S021:2026-09-23:long",
             "side": "buy", "volume": 1}]
    client = _FakeClient(positions=stale)
    _patch_adapter(monkeypatch, "bot.ctrader_orb", "CTraderORB", client)
    monkeypatch.setattr(pa, "StrategyLogger", lambda *a, **k: _NullLogger())

    n = pa.audit_account_strategy(5)
    assert n == 1
    assert client.closed == [(675236786, 1)]


def test_does_not_touch_a_sibling_strategys_position(monkeypatch, tmp_path):
    """S007 and S021 share one cTrader account -- S007's audit must never
    close an S021-labeled position it happens to see there, and vice versa."""
    link = _fake_link("S021")
    _patch_db(monkeypatch, link)
    mixed = [
        {"position_id": 1, "label": "S021:2026-09-23:long", "side": "buy", "volume": 1},
        {"position_id": 2, "label": "S007:2026-09-23:6", "side": "sell", "volume": 1},
    ]
    client = _FakeClient(positions=mixed)
    _patch_adapter(monkeypatch, "bot.ctrader_orb", "CTraderORB", client)
    monkeypatch.setattr(pa, "StrategyLogger", lambda *a, **k: _NullLogger())

    n = pa.audit_account_strategy(5)
    assert n == 1
    assert client.closed == [(1, 1)]  # only the S021-owned one


def test_s007_uses_ctrader_s007_adapter(monkeypatch, tmp_path):
    link = _fake_link("S007")
    _patch_db(monkeypatch, link)
    stale = [{"position_id": 111, "label": "S007:2026-09-23:6", "side": "sell", "volume": 1}]
    client = _FakeClient(positions=stale)
    _patch_adapter(monkeypatch, "bot.ctrader_s007", "CTraderS007", client)
    monkeypatch.setattr(pa, "StrategyLogger", lambda *a, **k: _NullLogger())

    n = pa.audit_account_strategy(1)
    assert n == 1
    assert client.closed == [(111, 1)]


class _NullLogger:
    """Swallows every StrategyLogger call -- these tests only care about
    audit_account_strategy's own return value/close-call behavior, not its
    logging side effects (already covered by every other strategy's own
    logging tests)."""
    def cycle_start(self, **kw): return "cid"
    def cycle_end(self, *a, **kw): pass
    def event(self, *a, **kw): pass
    def error(self, *a, **kw): pass
    def order(self, *a, **kw): pass
    def position(self, *a, **kw): pass
