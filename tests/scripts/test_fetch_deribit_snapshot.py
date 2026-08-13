"""Tests for scripts/fetch_deribit_snapshot.py (ALGODEV-9).

No live network -- every Deribit call is mocked. The `_isolate_paths`
autouse fixture reroutes STATE_PATH/OUT_ROOT/LOG_ROOT to tmp_path for the
whole module: the exact class of leak the spec (infra-scheduled-jobs-spec.md
rev.2 SS2) calls out after the 2026-08-08 S009 ledger incident, where a
test wrote into a live reports/ file because only one of eleven tests
patched the path. Every test here gets a fresh tmp_path regardless of
which fixture it explicitly asks for.
"""
from __future__ import annotations

import datetime

import pandas as pd
import pytest

from scripts import fetch_deribit_snapshot as mod
from utils.strategy_state import FileStateStore
from utils.trade_logger import StrategyLogger

NOW = datetime.datetime(2026, 8, 12, 16, 0, 0, tzinfo=datetime.timezone.utc)

BOOK_SUMMARY_ROW = {
    "instrument_name": "BTC-29AUG26-60000-C",
    "bid_price": 0.01, "ask_price": 0.012, "mid_price": 0.011, "mark_price": 0.0111,
    "last": 0.0109, "mark_iv": 55.2, "open_interest": 120.5, "volume": 3.1,
    "volume_usd": 210000.0, "underlying_price": 61000.0, "underlying_index": "BTC-29AUG26",
    "interest_rate": 0.0, "estimated_delivery_price": 61000.0, "creation_timestamp": 1_700_000_000_000,
}
INSTRUMENT_ROW = {
    "instrument_name": "BTC-29AUG26-60000-C",
    "strike": 60000.0, "option_type": "call", "expiration_timestamp": 1_756_483_200_000,
    "tick_size": 0.0001, "min_trade_amount": 0.1, "maker_commission": 0.0003,
    "taker_commission": 0.0003,
}


@pytest.fixture(autouse=True)
def _isolate_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(mod, "STATE_PATH", tmp_path / "state" / "deribit_snapshot.json")
    monkeypatch.setattr(mod, "OUT_ROOT", tmp_path / "raw" / "deribit_options")
    monkeypatch.setattr(mod, "LOG_ROOT", tmp_path / "logs")
    yield


@pytest.fixture
def log(tmp_path):
    return StrategyLogger(mod.LOG_GROUP, log_root=str(tmp_path / "logs"), console=False)


@pytest.fixture
def state_store(tmp_path):
    return FileStateStore(tmp_path / "state.json", mod._default_state)


def _mock_deribit(monkeypatch, book_summary=None, instruments=None, ticker=None,
                  raise_for=None):
    """raise_for: {"public/get_book_summary_by_currency": requests.RequestException(...)}
    lets a test target one endpoint's failure without touching the others."""
    import requests as _requests

    book_summary = book_summary if book_summary is not None else [BOOK_SUMMARY_ROW]
    instruments = instruments if instruments is not None else [INSTRUMENT_ROW]
    raise_for = raise_for or {}

    class _Resp:
        def __init__(self, result):
            self._result = result
        def raise_for_status(self):
            pass
        def json(self):
            return {"result": self._result}

    def _fake_get(url, params=None, timeout=None):
        for method, exc in raise_for.items():
            if url.endswith(method):
                raise exc
        if url.endswith("public/get_book_summary_by_currency"):
            return _Resp(book_summary)
        if url.endswith("public/get_instruments"):
            return _Resp(instruments)
        if url.endswith("public/ticker"):
            name = params["instrument_name"]
            row = next((t for t in (ticker or []) if t["instrument_name"] == name), None)
            return _Resp(row or {"bid_iv": None, "ask_iv": None, "greeks": {}})
        raise AssertionError(f"unexpected URL {url}")

    monkeypatch.setattr(_requests, "get", _fake_get)


def test_first_run_writes_partition_and_advances_watermark(monkeypatch, tmp_path, log, state_store):
    _mock_deribit(monkeypatch)
    code = mod.run(currencies=("BTC",), now=NOW, log=log, state_store=state_store,
                   out_root=tmp_path / "out")
    assert code == mod.EXIT_OK

    written = tmp_path / "out" / "date=2026-08-12" / "BTC.parquet"
    assert written.exists()
    df = pd.read_parquet(written)
    assert len(df) == 1
    assert df.iloc[0]["instrument_name"] == "BTC-29AUG26-60000-C"
    assert df.iloc[0]["strike"] == 60000.0          # joined field present
    assert "delta" not in df.columns                # no greeks by default

    state = state_store.load()
    assert state["last_snapshot_date"] == "2026-08-12"
    assert state["last_attempt_ts"] is not None


def test_second_run_same_day_is_noop(monkeypatch, tmp_path, log, state_store):
    _mock_deribit(monkeypatch)
    mod.run(currencies=("BTC",), now=NOW, log=log, state_store=state_store,
           out_root=tmp_path / "out")

    calls = {"n": 0}
    import requests as _requests
    real_get = _requests.get
    def _counting_get(*a, **kw):
        calls["n"] += 1
        return real_get(*a, **kw)
    monkeypatch.setattr(_requests, "get", _counting_get)

    code = mod.run(currencies=("BTC",), now=NOW, log=log, state_store=state_store,
                   out_root=tmp_path / "out")
    assert code == mod.EXIT_OK
    assert calls["n"] == 0                           # watermark short-circuits, no network


def test_force_recaptures_same_day(monkeypatch, tmp_path, log, state_store):
    _mock_deribit(monkeypatch)
    mod.run(currencies=("BTC",), now=NOW, log=log, state_store=state_store, out_root=tmp_path / "out")
    code = mod.run(currencies=("BTC",), now=NOW, log=log, state_store=state_store,
                   out_root=tmp_path / "out", force=True)
    assert code == mod.EXIT_OK


def test_duplicate_instrument_name_in_join_is_fatal(monkeypatch, tmp_path, log, state_store):
    dup_book = [BOOK_SUMMARY_ROW, dict(BOOK_SUMMARY_ROW)]        # same instrument twice
    dup_instr = [INSTRUMENT_ROW, dict(INSTRUMENT_ROW)]
    _mock_deribit(monkeypatch, book_summary=dup_book, instruments=dup_instr)
    code = mod.run(currencies=("BTC",), now=NOW, log=log, state_store=state_store,
                   out_root=tmp_path / "out")
    assert code == mod.EXIT_FATAL
    assert state_store.load()["last_snapshot_date"] is None


def test_dry_run_writes_nothing_and_does_not_move_watermark(monkeypatch, tmp_path, log, state_store):
    _mock_deribit(monkeypatch)
    code = mod.run(currencies=("BTC",), now=NOW, log=log, state_store=state_store,
                   out_root=tmp_path / "out", dry_run=True)
    assert code == mod.EXIT_OK
    assert not (tmp_path / "out").exists()
    assert state_store.load()["last_snapshot_date"] is None


def test_join_drops_instrument_missing_from_either_side(monkeypatch, tmp_path, log, state_store):
    extra_book_only = dict(BOOK_SUMMARY_ROW, instrument_name="BTC-29AUG26-70000-C")
    _mock_deribit(monkeypatch, book_summary=[BOOK_SUMMARY_ROW, extra_book_only],
                 instruments=[INSTRUMENT_ROW])  # 70000-C absent from instruments
    code = mod.run(currencies=("BTC",), now=NOW, log=log, state_store=state_store,
                   out_root=tmp_path / "out")
    assert code == mod.EXIT_OK
    df = pd.read_parquet(tmp_path / "out" / "date=2026-08-12" / "BTC.parquet")
    assert len(df) == 1
    assert df.iloc[0]["instrument_name"] == "BTC-29AUG26-60000-C"


def test_with_greeks_adds_columns(monkeypatch, tmp_path, log, state_store):
    ticker = [{"instrument_name": "BTC-29AUG26-60000-C", "bid_iv": 54.0, "ask_iv": 56.0,
              "greeks": {"delta": 0.41}}]
    _mock_deribit(monkeypatch, ticker=ticker)
    code = mod.run(currencies=("BTC",), now=NOW, log=log, state_store=state_store,
                   out_root=tmp_path / "out", with_greeks=True)
    assert code == mod.EXIT_OK
    df = pd.read_parquet(tmp_path / "out" / "date=2026-08-12" / "BTC.parquet")
    assert df.iloc[0]["delta"] == 0.41
    assert df.iloc[0]["bid_iv"] == 54.0


def test_transient_network_error_exits_1_and_preserves_watermark(monkeypatch, tmp_path, log, state_store):
    import requests as _requests
    _mock_deribit(monkeypatch, raise_for={
        "public/get_book_summary_by_currency": _requests.ConnectionError("boom"),
    })
    code = mod.run(currencies=("BTC",), now=NOW, log=log, state_store=state_store,
                   out_root=tmp_path / "out")
    assert code == mod.EXIT_TRANSIENT
    state = state_store.load()
    assert state["last_snapshot_date"] is None       # not moved
    assert state["last_attempt_ts"] is not None       # but the attempt IS recorded
    assert not (tmp_path / "out").exists()


def test_fatal_contract_error_exits_2(monkeypatch, tmp_path, log, state_store):
    broken_row = {k: v for k, v in BOOK_SUMMARY_ROW.items() if k != "mark_iv"}
    _mock_deribit(monkeypatch, book_summary=[broken_row])
    code = mod.run(currencies=("BTC",), now=NOW, log=log, state_store=state_store,
                   out_root=tmp_path / "out")
    assert code == mod.EXIT_FATAL
    assert state_store.load()["last_snapshot_date"] is None


def test_missing_result_envelope_is_fatal(monkeypatch, tmp_path, log, state_store):
    import requests as _requests

    class _BadResp:
        def raise_for_status(self):
            pass
        def json(self):
            return {"error": "nope"}

    monkeypatch.setattr(_requests, "get", lambda *a, **kw: _BadResp())
    code = mod.run(currencies=("BTC",), now=NOW, log=log, state_store=state_store,
                   out_root=tmp_path / "out")
    assert code == mod.EXIT_FATAL


def test_cli_rejects_unknown_since_flag(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["fetch_deribit_snapshot.py", "--since", "2026-01-01"])
    with pytest.raises(SystemExit) as exc_info:
        mod.main()
    assert exc_info.value.code == 2                  # argparse's own "unrecognized arguments"


def test_atomic_write_leaves_no_partial_file_on_failure(monkeypatch, tmp_path, log, state_store):
    _mock_deribit(monkeypatch)

    def _boom(self, path, index=False):
        raise OSError("disk full (simulated)")
    monkeypatch.setattr(pd.DataFrame, "to_parquet", _boom)

    with pytest.raises(OSError):
        mod.run(currencies=("BTC",), now=NOW, log=log, state_store=state_store,
               out_root=tmp_path / "out")

    partition_dir = tmp_path / "out" / "date=2026-08-12"
    leftover = list(partition_dir.glob("*")) if partition_dir.exists() else []
    assert not any(p.suffix == ".parquet" for p in leftover)
    assert not any(p.name.endswith(".tmp") for p in leftover)  # temp file cleaned up
