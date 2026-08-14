"""bot/s009_paper.py::run_cycle_for_account() -- the account-parameterized
S009 cycle extracted from run_once() so a DB-driven multi-account caller
(webapp/runner.py) and the single-account CLI share one implementation
(mirrors bot/s007_paper.py::run_cycle_for_account(), see that module's own
tests for the sibling pattern).

The funding-carry engine itself (_engine/forward_target_book) is monkey-
patched to fixed, deterministic values -- these tests are about the
multi-account plumbing this refactor changed (state isolation, creds
passthrough, error containment, run_once()'s unchanged behavior), not about
re-validating the strategy engine, which has no test fixtures of its own
yet and is out of scope here.
"""
from __future__ import annotations

import sys
import types

import pandas as pd
import pytest

import bot.s009_paper as s009
from utils.strategy_state import FileStateStore


# --- fakes -------------------------------------------------------------

FAKE_DAY = 20000   # arbitrary past day-index (ms-since-epoch // 86_400_000)


def _fake_engine(data_dir, cfg):
    out = pd.DataFrame({"net_ret": [0.01, 0.02], "turnover": [0.1, 0.1], "n_pos": [4, 4]},
                       index=[FAKE_DAY, FAKE_DAY + 1])
    price_comp = pd.Series([0.005, 0.01], index=out.index)
    fund_comp = pd.Series([0.005, 0.01], index=out.index)
    return pd.DataFrame(), pd.DataFrame(), out, pd.DataFrame(), price_comp, fund_comp


def _fake_target_book(close, funding, cfg):
    return {"BTCUSDT": 0.3, "ETHUSDT": -0.3}


@pytest.fixture(autouse=True)
def _stub_engine(monkeypatch):
    monkeypatch.setattr(s009, "_engine", _fake_engine)
    monkeypatch.setattr(s009, "forward_target_book", _fake_target_book)
    monkeypatch.setattr(s009, "refresh_data", lambda *a, **k: None)


@pytest.fixture(autouse=True)
def _isolate_paper_dirs(tmp_path, monkeypatch):
    """No test may touch the LIVE reports/paper_s009/ tree.

    run_cycle_for_account() books each closed day through append_ledger(),
    which resolves LEDGER_FILE/STATE_DIR from module globals -- so without
    this fixture a plain `pytest` run appended fake rows (FAKE_DAY + 1,
    equity 1.02) straight into the running paper bot's ledger.csv and
    corrupted the only record of live performance (found 2026-08-08: three
    such rows were already there, and `--reconcile` reads exactly this file).

    Same class of leak, and same fix, as the S007 positions-log cleanup
    fixture -- see decisions-log.md 2026-07-21.
    """
    paper_dir = tmp_path / "paper_s009"
    paper_dir.mkdir()
    monkeypatch.setattr(s009, "REPO", tmp_path)
    monkeypatch.setattr(s009, "STATE_DIR", paper_dir)
    monkeypatch.setattr(s009, "STATE_FILE", paper_dir / "state.json")
    monkeypatch.setattr(s009, "LEDGER_FILE", paper_dir / "ledger.csv")


class _FakeBybitExec:
    """Stands in for bot.bybit_exec.BybitExec -- captures constructor args
    for the creds-passthrough assertion, no network."""
    last_init_kwargs = None

    def __init__(self, **kwargs):
        _FakeBybitExec.last_init_kwargs = kwargs
        self.env = "testnet"

    def wallet_equity(self):
        return 100.0

    def positions(self):
        return {}

    def ticker_price(self, sym):
        return 100.0

    def instrument(self, sym):
        return types.SimpleNamespace(symbol=sym, qty_step=0.01, min_qty=0.01, tick_size=0.01)

    def place_market(self, sym, side, qty):
        return {"orderId": "abc123"}

    def recent_fill_price(self, sym, oid):
        return 100.0


@pytest.fixture
def fake_bybit(monkeypatch):
    fake_mod = types.SimpleNamespace(BybitExec=_FakeBybitExec)
    monkeypatch.setitem(sys.modules, "bot.bybit_exec", fake_mod)
    _FakeBybitExec.last_init_kwargs = None
    yield _FakeBybitExec


def _log(tmp_path, name="S009TEST"):
    from utils.trade_logger import StrategyLogger
    return StrategyLogger(name, log_root=str(tmp_path), console=False)


def _store(tmp_path, name="state.json"):
    return FileStateStore(tmp_path / name, default_factory=lambda: {"last_day": None, "equity": 1.0, "book": {}})


# --- run_cycle_for_account: core plumbing -------------------------------

def test_first_run_seeds_book_without_booking_any_day(tmp_path):
    state = _store(tmp_path)
    result = s009.run_cycle_for_account(
        account_key="acct-a", creds=None, cfg=s009.DEPLOY, state=state, logger=_log(tmp_path),
        do_fetch=False, drop_forming=False, broker="off")

    assert result["error"] is None
    assert result["booked"] == 0                       # first run: seed only, book nothing yet
    assert result["target"] == {"BTCUSDT": 0.3, "ETHUSDT": -0.3}
    saved = state.load()
    assert saved["last_day"] == FAKE_DAY + 1
    assert saved["book"] == {"BTCUSDT": 0.3, "ETHUSDT": -0.3}


def test_second_run_books_the_new_closed_day(tmp_path):
    state = _store(tmp_path)
    state.save({"last_day": FAKE_DAY, "equity": 1.0, "book": {}})   # already caught up to FAKE_DAY
    result = s009.run_cycle_for_account(
        account_key="acct-a", creds=None, cfg=s009.DEPLOY, state=state, logger=_log(tmp_path),
        do_fetch=False, drop_forming=False, broker="off")

    assert result["booked"] == 1                       # only FAKE_DAY+1 was new
    assert result["equity"] == pytest.approx(1.02)      # 1.0 * (1 + 0.02)


def test_two_accounts_do_not_collide_on_state(tmp_path):
    state_a = _store(tmp_path, "a.json")
    state_b = _store(tmp_path, "b.json")
    s009.run_cycle_for_account(account_key="a", creds=None, cfg=s009.DEPLOY, state=state_a,
                               logger=_log(tmp_path, "A"), do_fetch=False, drop_forming=False, broker="off")

    # b's state must still be untouched -- this is exactly the collision the
    # old single shared reports/paper_s009/state.json had.
    assert state_b.load() == {"last_day": None, "equity": 1.0, "book": {}}


def test_cycle_start_and_end_are_always_paired(tmp_path):
    log = _log(tmp_path)
    s009.run_cycle_for_account(account_key="a", creds=None, cfg=s009.DEPLOY, state=_store(tmp_path),
                               logger=log, do_fetch=False, drop_forming=False, broker="off")
    events = (tmp_path / "S009TEST" / f"events-{pd.Timestamp.now().date().isoformat()}.jsonl").read_text()
    assert '"kind": "cycle_start"' in events
    assert '"kind": "cycle_end"' in events


# --- error containment ---------------------------------------------------

def test_engine_exception_is_caught_and_reported_not_raised(tmp_path, monkeypatch):
    monkeypatch.setattr(s009, "_engine", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    result = s009.run_cycle_for_account(
        account_key="a", creds=None, cfg=s009.DEPLOY, state=_store(tmp_path), logger=_log(tmp_path),
        do_fetch=False, drop_forming=False, broker="off")
    assert result["error"] is not None
    assert "boom" in result["error"]
    assert result["booked"] == 0


def test_no_closed_day_returns_cleanly_with_no_error(tmp_path, monkeypatch):
    monkeypatch.setattr(s009, "_engine", lambda *a, **k: (
        pd.DataFrame(), pd.DataFrame(), pd.DataFrame(columns=["net_ret", "turnover", "n_pos"]),
        pd.DataFrame(), pd.Series(dtype=float), pd.Series(dtype=float)))
    result = s009.run_cycle_for_account(
        account_key="a", creds=None, cfg=s009.DEPLOY, state=_store(tmp_path), logger=_log(tmp_path),
        do_fetch=False, drop_forming=False, broker="off")
    assert result["error"] is None
    assert result["date"] is None


# --- creds passthrough (broker path) -------------------------------------

def test_creds_are_passed_straight_to_bybitexec(tmp_path, fake_bybit):
    s009.run_cycle_for_account(
        account_key="acct-a", creds={"api_key": "k", "api_secret": "s"}, cfg=s009.DEPLOY,
        state=_store(tmp_path), logger=_log(tmp_path), do_fetch=False, drop_forming=False,
        broker="dry", allow_mainnet=False)
    assert fake_bybit.last_init_kwargs == {
        "api_key": "k", "api_secret": "s", "env": None, "allow_mainnet": False}


def test_no_creds_falls_back_to_accounts_yml_by_account_key(tmp_path, fake_bybit):
    s009.run_cycle_for_account(
        account_key="Bybit-algo009", creds=None, cfg=s009.DEPLOY, state=_store(tmp_path),
        logger=_log(tmp_path), do_fetch=False, drop_forming=False, broker="dry", allow_mainnet=False)
    assert fake_bybit.last_init_kwargs == {"name": "Bybit-algo009", "env": None, "allow_mainnet": False}


def test_env_is_passed_straight_to_bybitexec(tmp_path, fake_bybit):
    """Found live 2026-08-13/14: run_cycle_for_account used to construct
    BybitExec with no env at all, so it always fell back to the
    BYBIT_TESTNET OS env var -- unset in the Ofelia dispatch container, so
    every DB-driven cycle silently hit api-testnet.bybit.com with
    mainnet-only credentials (401 Client Error: API key is invalid) for a
    real-money mainnet account. env must reach BybitExec unchanged."""
    s009.run_cycle_for_account(
        account_key="acct-a", creds={"api_key": "k", "api_secret": "s"}, cfg=s009.DEPLOY,
        state=_store(tmp_path), logger=_log(tmp_path), do_fetch=False, drop_forming=False,
        broker="dry", allow_mainnet=False, env="mainnet")
    assert fake_bybit.last_init_kwargs["env"] == "mainnet"


def test_broker_off_never_touches_bybitexec(tmp_path, fake_bybit):
    s009.run_cycle_for_account(
        account_key="a", creds={"api_key": "k", "api_secret": "s"}, cfg=s009.DEPLOY,
        state=_store(tmp_path), logger=_log(tmp_path), do_fetch=False, drop_forming=False, broker="off")
    assert fake_bybit.last_init_kwargs is None


# --- run_once(): unchanged behavior for the live single-account path -----

def test_run_once_uses_the_module_level_state_store(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(s009, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(s009, "_state_store",
                        FileStateStore(tmp_path / "state.json", default_factory=s009._default_state))
    monkeypatch.setattr(s009, "LEDGER_FILE", tmp_path / "ledger.csv")
    monkeypatch.setattr(s009, "REPO", tmp_path)

    s009.run_once(s009.DATA_DIR, s009.DEPLOY, do_fetch=False, drop_forming=False, broker="off")

    assert (tmp_path / "state.json").exists()
    out = capsys.readouterr().out
    assert "TARGET BOOK" in out
    assert "BTCUSDT" in out


# --- ledger.csv: per-caller opt-in, not a shared-by-default file ---------
#
# Regression coverage for a real incident found 2026-08-08: append_ledger()
# used to always write the module-global LEDGER_FILE regardless of which
# `state` was passed in, so even after state.json was made per-account, the
# DB-driven multi-account path would still have every account's day_pnl
# rows land in the SAME reports/paper_s009/ledger.csv -- exactly the
# collision the state.json refactor was supposed to eliminate. A run of
# this very test file (before the fix + the _isolate_paper_dirs fixture
# above existed) wrote fake rows into the real live ledger.

def test_run_once_writes_to_the_ledger_file(tmp_path, monkeypatch):
    monkeypatch.setattr(s009, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(s009, "_state_store",
                        FileStateStore(tmp_path / "state.json", default_factory=s009._default_state))
    monkeypatch.setattr(s009, "LEDGER_FILE", tmp_path / "ledger.csv")
    monkeypatch.setattr(s009, "REPO", tmp_path)
    s009._state_store.save({"last_day": FAKE_DAY, "equity": 1.0, "book": {}})  # so the next run books a day

    s009.run_once(s009.DATA_DIR, s009.DEPLOY, do_fetch=False, drop_forming=False, broker="off")

    assert (tmp_path / "ledger.csv").exists()
    assert f"{FAKE_DAY + 1}," in (tmp_path / "ledger.csv").read_text()


def test_run_cycle_for_account_without_ledger_file_writes_no_csv(tmp_path):
    state = _store(tmp_path)
    state.save({"last_day": FAKE_DAY, "equity": 1.0, "book": {}})
    s009.run_cycle_for_account(
        account_key="acct-a", creds=None, cfg=s009.DEPLOY, state=state, logger=_log(tmp_path),
        do_fetch=False, drop_forming=False, broker="off")   # no ledger_file -- the DB-driven default

    assert list(tmp_path.glob("*.csv")) == []
