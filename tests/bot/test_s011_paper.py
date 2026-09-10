"""ALGODEV-34: bot/s011_paper.py::run_cycle_for_account must not record
prev_held/position_value as if a broker action succeeded when it didn't.
Plus (2026-09-09) the stale-D1-feed guard and the ledger-write-path fix
that share the same fake-client harness.

Found live twice: ESTOXX50's close rejected 2026-08-27 (MARKET_CLOSED)
stayed open at the broker for days while the bot's own state believed it
was flat (no retry ever generated, since the transition prev_held=1 ->
held=0 had already been "consumed" by the first, failed attempt); DOW/
RUSSELL's opens rejected 2026-09-01 00:00 (MARKET_CLOSED, midnight tick)
never retried for the same reason, despite the broker never actually
holding them.

These tests exercise run_cycle_for_account's own bookkeeping with a fake
CTraderS011 (no network, no real cTrader session) that lets a test control
which of the broker_actions decide() returns should be reported as
succeeded vs failed.
"""
from __future__ import annotations

import os

os.environ.setdefault("APP_SECRET_KEY", "test-only-not-a-real-key")

import sys
import types
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

import bot.s011_paper as s011
from strategies.rsi2_portfolio import PortfolioConfig
from utils.strategy_state import FileStateStore


def _cfg(**overrides):
    # A small, deterministic config -- start_capital/cap_pct chosen so
    # position sizes are easy to reason about by hand. commission/spread
    # zeroed so cost_rate (a derived property) works out to exactly 0.
    return PortfolioConfig(start_capital=1000.0, cap_pct=0.25,
                           commission_bps=0.0, spread_bps=0.0, **overrides)


# Fixed "last closed trading day" every test pins
# s011._expected_last_closed_trading_date() to, so the stale-D1-feed guard
# is deterministic instead of depending on the wall clock. Both dates are
# in the past, so decide()'s forming-bar drop (index.date < today_utc)
# never trims the fixture bars no matter when the suite runs.
EXPECTED_TRADING_DATE = "2026-09-04"   # Friday -- the day a caught-up feed has
NEXT_TRADING_DATE = "2026-09-07"       # the following trading day
STALE_TRADING_DATE = "2026-09-01"      # a lagging broker feed, days behind

# Sentinel for _run's `ledger_file`: "use tmp_path/ledger.csv" -- distinct
# from an explicit None, which is the DB-driven caller's no-ledger path.
_TMP_LEDGER = object()


def _bars(n=3, last=EXPECTED_TRADING_DATE):
    idx = pd.date_range(end=last, periods=n, freq="D")
    return pd.DataFrame({"close": [100.0] * n, "high": [101.0] * n, "low": [99.0] * n,
                         "open": [100.0] * n}, index=idx)


class _FakeClient:
    """Stands in for bot.ctrader_s011.CTraderS011 -- run_live_cycle_multi
    calls `decide` once with fixture daily_bars/positions/resolved, then
    reports each returned broker_action as succeeded or failed per the
    test-controlled `fail_assets` set (mirroring what a real MARKET_CLOSED
    rejection looks like from run_cycle_for_account's point of view)."""

    def __init__(self, held_by_asset, existing_positions, resolved, fail_assets,
                 bars_last_date=EXPECTED_TRADING_DATE, **kwargs):
        self._held = held_by_asset
        self._positions = existing_positions
        self._resolved = resolved
        self._fail_assets = fail_assets
        self._bars_last_date = bars_last_date

    def run_live_cycle_multi(self, candidates_by_asset, history_days, decide):
        daily_bars = {a: _bars(last=self._bars_last_date) for a in self._held}
        broker_actions = decide(daily_bars, self._positions, 1000.0, {}, {}, self._resolved) or []
        results = []
        for a in broker_actions:
            asset = a["asset"]
            if asset in self._fail_assets:
                results.append({"action": a, "result": None,
                                "error": RuntimeError("MARKET_CLOSED: Trading is not available")})
            else:
                results.append({"action": a, "result": {"orderId": "1"}, "error": None})
        return dict(resolved=self._resolved, unresolved=[], daily_bars=daily_bars,
                   positions=self._positions, actions=broker_actions, results=results, balance=1000.0)


@pytest.fixture
def fake_rsi(monkeypatch):
    """rsi2_signal is monkeypatched to just read the last-bar close through
    a test-controlled {asset: 0|1} map stashed on the fake bars -- the real
    RSI(2) math is irrelevant to this module's state-bookkeeping logic."""
    held_map = {}

    def fake_signal(bars, cfg):
        # Recover which asset this call is for isn't directly possible from
        # bars alone, so tests instead monkeypatch per-call via closures
        # below (see _install_held). Default: everyone flat.
        return pd.Series([0], index=bars.index[-1:])

    monkeypatch.setattr(s011, "rsi2_signal", fake_signal)
    return held_map


def _log(tmp_path, name="S011TEST"):
    from utils.trade_logger import StrategyLogger
    return StrategyLogger(name, log_root=str(tmp_path), console=False)


def _read_events(tmp_path, name="S011TEST"):
    import json
    path = tmp_path / name / f"events-{pd.Timestamp.now().date().isoformat()}.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _run(tmp_path, monkeypatch, *, held_by_asset, existing_positions, resolved,
         fail_assets, prev_held, position_value, cash,
         bars_last_date=EXPECTED_TRADING_DATE, ledger_file=_TMP_LEDGER):
    """Wires a fake CTraderS011 + a per-asset RSI stub, then runs one cycle.

    `ledger_file` defaults to a throwaway file under tmp_path so no test can
    ever append to the REAL reports/paper_s011/ledger.csv again (it used to:
    run_cycle_for_account hardcoded the module-global LEDGER_FILE, so this
    suite's 2026-01-03 fixture rows were landing in the live ledger) -- pass
    None explicitly to exercise the DB-driven caller's no-ledger path.
    """
    monkeypatch.setattr(s011, "_expected_last_closed_trading_date",
                        lambda *a, **k: EXPECTED_TRADING_DATE)

    def fake_rsi2_signal(bars, cfg):
        # Each fake asset's bars all share the SAME dummy content, so route
        # on identity of the dict entry via a side-channel index instead.
        idx = fake_rsi2_signal.calls
        fake_rsi2_signal.calls += 1
        asset = list(held_by_asset)[idx % len(held_by_asset)]
        return pd.Series([held_by_asset[asset]], index=bars.index[-1:])
    fake_rsi2_signal.calls = 0
    monkeypatch.setattr(s011, "rsi2_signal", fake_rsi2_signal)

    fake_mod = types.SimpleNamespace(
        CTraderS011=lambda **kw: _FakeClient(held_by_asset, existing_positions, resolved,
                                            fail_assets, bars_last_date=bars_last_date))
    monkeypatch.setitem(sys.modules, "bot.ctrader_s011", fake_mod)

    state = FileStateStore(tmp_path / "state.json", default_factory=s011._default_state)
    state.save({"last_date": "2026-08-31", "cash": cash, "equity": cash + sum(position_value.values()),
               "position_value": position_value, "prev_held": prev_held})

    if ledger_file is _TMP_LEDGER:
        ledger_file = tmp_path / "ledger.csv"
    cfg = _cfg()
    result = s011.run_cycle_for_account(
        account_key="acct-a", creds={"api_key": "k"}, cfg=cfg, state=state,
        logger=_log(tmp_path), broker="execute", allow_mainnet=False,
        candidates={a: (a,) for a in held_by_asset}, ledger_file=ledger_file)
    return result, state.load()


def test_failed_close_does_not_mark_asset_as_closed(tmp_path, monkeypatch):
    """ESTOXX50-shaped case: prev_held=1, signal now wants held=0, the
    close order fails -- state must still show it held (so a later cycle
    retries), not flat."""
    result, st = _run(
        tmp_path, monkeypatch,
        held_by_asset={"ESTOXX50": 0},
        existing_positions=[{"label": "S011:ESTOXX50:2026-08-27", "position_id": 1, "volume": 100}],
        resolved={"ESTOXX50": "STOXX50"},
        fail_assets={"ESTOXX50"},
        prev_held={"ESTOXX50": 1}, position_value={"ESTOXX50": 300.0}, cash=700.0,
    )
    assert result["error"] is None
    assert st["prev_held"]["ESTOXX50"] == 1          # NOT reverted to closed
    assert st["position_value"]["ESTOXX50"] == 300.0  # notional untouched
    assert st["cash"] == 700.0                        # no phantom cash credited


def test_failed_open_does_not_mark_asset_as_opened(tmp_path, monkeypatch):
    """DOW/RUSSELL-shaped case: prev_held=0, signal now wants held=1, the
    open order fails (MARKET_CLOSED) -- state must still show it flat, so
    the next cycle with a real transition tries again."""
    result, st = _run(
        tmp_path, monkeypatch,
        held_by_asset={"DOW": 1},
        existing_positions=[],
        resolved={"DOW": "US30"},
        fail_assets={"DOW"},
        prev_held={"DOW": 0}, position_value={}, cash=1000.0,
    )
    assert result["error"] is None
    assert st["prev_held"]["DOW"] == 0
    assert st.get("position_value", {}).get("DOW", 0.0) == 0.0
    assert st["cash"] == 1000.0                       # nothing spent


def test_next_cycle_retries_a_previously_failed_open(tmp_path, monkeypatch):
    """Direct regression for the live incident: a failed open must produce
    a fresh open action on the VERY NEXT cycle (same held=1 signal), not
    silence forever because prev_held was wrongly advanced."""
    _, st_after_failure = _run(
        tmp_path, monkeypatch,
        held_by_asset={"DOW": 1}, existing_positions=[], resolved={"DOW": "US30"},
        fail_assets={"DOW"}, prev_held={"DOW": 0}, position_value={}, cash=1000.0,
    )
    assert st_after_failure["prev_held"]["DOW"] == 0

    # Second cycle: same signal (held=1), this time the broker accepts it.
    # The calendar (and the feed with it) has moved on one trading day --
    # otherwise the up-to-date short-circuit, not the retry, would run.
    monkeypatch.setattr(s011, "_expected_last_closed_trading_date",
                        lambda *a, **k: NEXT_TRADING_DATE)

    def fake_rsi2_signal(bars, cfg):
        return pd.Series([1], index=bars.index[-1:])
    monkeypatch.setattr(s011, "rsi2_signal", fake_rsi2_signal)
    fake_mod = types.SimpleNamespace(
        CTraderS011=lambda **kw: _FakeClient({"DOW": 1}, [], {"DOW": "US30"}, set(),
                                             bars_last_date=NEXT_TRADING_DATE))
    monkeypatch.setitem(sys.modules, "bot.ctrader_s011", fake_mod)

    state = FileStateStore(tmp_path / "state.json", default_factory=s011._default_state)
    result2 = s011.run_cycle_for_account(
        account_key="acct-a", creds={"api_key": "k"}, cfg=_cfg(), state=state,
        logger=_log(tmp_path), broker="execute", allow_mainnet=False,
        candidates={"DOW": ("DOW",)}, ledger_file=tmp_path / "ledger.csv")

    assert result2["error"] is None
    assert any(a["asset"] == "DOW" and a["kind"] == "open" for a in result2["actions"])
    st_after_retry = state.load()
    assert st_after_retry["prev_held"]["DOW"] == 1
    assert st_after_retry["position_value"]["DOW"] > 0


def test_mixed_success_and_failure_only_reverts_the_failed_asset(tmp_path, monkeypatch):
    """Two assets transition the same cycle; one succeeds, one fails -- only
    the failed one's state should be reverted, the successful one commits
    normally (this exercises the merged-recompute path with a partial
    failure set, not just the single-asset cases above)."""
    result, st = _run(
        tmp_path, monkeypatch,
        held_by_asset={"DOW": 1, "RUSSELL": 1},
        existing_positions=[], resolved={"DOW": "US30", "RUSSELL": "US2000"},
        fail_assets={"RUSSELL"},
        prev_held={"DOW": 0, "RUSSELL": 0}, position_value={}, cash=1000.0,
    )
    assert result["error"] is None
    assert st["prev_held"]["DOW"] == 1
    assert st["position_value"]["DOW"] > 0
    assert st["prev_held"]["RUSSELL"] == 0
    assert st.get("position_value", {}).get("RUSSELL", 0.0) == 0.0


def test_all_actions_succeed_state_matches_pre_fix_behavior(tmp_path, monkeypatch):
    """No failures -- the fix must be a no-op vs. the original behavior."""
    result, st = _run(
        tmp_path, monkeypatch,
        held_by_asset={"DOW": 1}, existing_positions=[], resolved={"DOW": "US30"},
        fail_assets=set(), prev_held={"DOW": 0}, position_value={}, cash=1000.0,
    )
    assert result["error"] is None
    assert result["booked"] is True
    assert st["prev_held"]["DOW"] == 1
    assert st["position_value"]["DOW"] == pytest.approx(250.0)  # cap_pct=0.25 * 1000
    assert st["cash"] == pytest.approx(750.0)


# --------------------------------------------------------------------------
# Ledger write path: the DB-driven multi-account caller must write NO CSV
# (mirrors bot/s009_paper.py's own ledger_file=None fix, 2026-08-08).
# --------------------------------------------------------------------------

def test_db_path_writes_no_ledger_csv(tmp_path, monkeypatch):
    """ledger_file=None (what webapp/runner.py::_worker_s011 passes) must
    book the day into `state` but leave no CSV anywhere -- before this fix
    every DB-driven cycle appended to the shared reports/paper_s011/
    ledger.csv, and this very test suite wrote its fixture rows there."""
    result, st = _run(
        tmp_path, monkeypatch,
        held_by_asset={"DOW": 1}, existing_positions=[], resolved={"DOW": "US30"},
        fail_assets=set(), prev_held={"DOW": 0}, position_value={}, cash=1000.0,
        ledger_file=None,
    )
    assert result["error"] is None
    assert result["booked"] is True
    assert st["prev_held"]["DOW"] == 1                 # state still committed
    assert st["position_value"]["DOW"] == pytest.approx(250.0)
    assert not list(tmp_path.rglob("*.csv"))


def test_cli_path_writes_the_ledger_csv(tmp_path, monkeypatch):
    """The counterpart: an explicit ledger_file (what run_once passes) still
    gets exactly one booked row, so the guard didn't disable the CLI path."""
    ledger_file = tmp_path / "cli-ledger.csv"
    result, _ = _run(
        tmp_path, monkeypatch,
        held_by_asset={"DOW": 1}, existing_positions=[], resolved={"DOW": "US30"},
        fail_assets=set(), prev_held={"DOW": 0}, position_value={}, cash=1000.0,
        ledger_file=ledger_file,
    )
    assert result["booked"] is True
    rows = pd.read_csv(ledger_file)
    assert len(rows) == 1
    assert rows["date"].iloc[0] == EXPECTED_TRADING_DATE


# --------------------------------------------------------------------------
# Stale-D1-feed guard (2026-09-09): the broker's D1 feed lagged the calendar
# by a trading day over the 2026-09-07 US-holiday week, so the up-to-date
# short-circuit never fired and every 15-min cycle re-acted on a stale bar.
# --------------------------------------------------------------------------

def test_stale_feed_skips_cycle(tmp_path, monkeypatch):
    """Newest D1 bar behind the expected last closed trading day -> a logged
    no-op: nothing booked, no state write, no ledger row."""
    result, st = _run(
        tmp_path, monkeypatch,
        held_by_asset={"DOW": 1}, existing_positions=[], resolved={"DOW": "US30"},
        fail_assets=set(), prev_held={"DOW": 0}, position_value={}, cash=1000.0,
        bars_last_date=STALE_TRADING_DATE,
    )
    assert result["error"] is None
    assert result["booked"] is False
    assert result["date"] is None
    assert result["actions"] == []

    # State untouched -- still the seed _run wrote, not an advanced book.
    assert st["last_date"] == "2026-08-31"
    assert st["prev_held"] == {"DOW": 0}
    assert st["cash"] == pytest.approx(1000.0)
    assert st.get("position_value", {}).get("DOW", 0.0) == 0.0

    assert not list(tmp_path.rglob("*.csv"))

    stale_events = [e for e in _read_events(tmp_path) if e["kind"] == s011.STALE_FEED_STATUS]
    assert len(stale_events) == 1
    assert stale_events[0]["newest_bar"] == STALE_TRADING_DATE
    assert stale_events[0]["expected"] == EXPECTED_TRADING_DATE


def test_fresh_feed_still_books(tmp_path, monkeypatch):
    """Newest D1 bar == the expected last closed trading day -> the stale
    branch must NOT be taken and the cycle books normally."""
    result, st = _run(
        tmp_path, monkeypatch,
        held_by_asset={"DOW": 1}, existing_positions=[], resolved={"DOW": "US30"},
        fail_assets=set(), prev_held={"DOW": 0}, position_value={}, cash=1000.0,
        bars_last_date=EXPECTED_TRADING_DATE,
    )
    assert result["error"] is None
    assert result["booked"] is True
    assert result["date"] == EXPECTED_TRADING_DATE
    assert st["last_date"] == EXPECTED_TRADING_DATE
    assert not [e for e in _read_events(tmp_path) if e["kind"] == s011.STALE_FEED_STATUS]


# --------------------------------------------------------------------------
# _expected_last_closed_trading_date (2026-09-09): now computed in UTC
# against the broker's own D1 close (BROKER_DAY_CLOSE_UTC_HOUR) rather than a
# naive 22:05 local cutover that was only correct in a Europe/Kyiv process.
# It must return the SESSION date, the same label
# CTraderS011._session_dated_index puts on the broker's D1 bars.
#
# Calendar these use: 2026-09-09 is a Wednesday, 2026-09-11 a Friday,
# 2026-09-12 a Saturday, 2026-09-13 a Sunday.
# --------------------------------------------------------------------------

def test_expected_date_before_the_broker_day_closes_is_the_previous_session():
    """20:00 UTC Wednesday -- Wednesday's broker day has NOT closed yet
    (21:00/22:00 UTC), so the newest fully closed session is Tuesday's."""
    now = datetime(2026, 9, 9, 20, 0, tzinfo=timezone.utc)
    assert s011._expected_last_closed_trading_date(now) == "2026-09-08"


def test_expected_date_after_the_broker_day_closes_is_today():
    """22:30 UTC Wednesday -- past BROKER_DAY_CLOSE_UTC_HOUR, so Wednesday's
    own session is now the newest closed one."""
    now = datetime(2026, 9, 9, 22, 30, tzinfo=timezone.utc)
    assert s011._expected_last_closed_trading_date(now) == "2026-09-09"


def test_expected_date_on_a_weekend_walks_back_to_friday():
    """10:00 UTC Saturday -> Friday 2026-09-11 (no weekend bars print)."""
    now = datetime(2026, 9, 12, 10, 0, tzinfo=timezone.utc)
    assert s011._expected_last_closed_trading_date(now) == "2026-09-11"


def test_expected_date_late_sunday_still_walks_back_to_friday():
    """23:00 UTC Sunday is past the broker-day close, so the naive answer
    would be Sunday itself -- the weekend walk-back must still apply."""
    now = datetime(2026, 9, 13, 23, 0, tzinfo=timezone.utc)
    assert s011._expected_last_closed_trading_date(now) == "2026-09-11"


def test_expected_date_tolerates_a_naive_datetime():
    """Older callers/tests pass a naive datetime -- it is read as UTC, not as
    the process-local time the pre-2026-09-09 version assumed."""
    assert s011._expected_last_closed_trading_date(
        datetime(2026, 9, 9, 22, 30)) == "2026-09-09"
    assert s011._expected_last_closed_trading_date(
        datetime(2026, 9, 9, 20, 0)) == "2026-09-08"


def test_expected_date_converts_a_non_utc_aware_datetime():
    """01:30 Kyiv (UTC+3) on Thursday == 22:30 UTC Wednesday -> Wednesday's
    session, i.e. the answer follows UTC, not the wall clock handed in."""
    kyiv_summer = timezone(timedelta(hours=3))
    now = datetime(2026, 9, 10, 1, 30, tzinfo=kyiv_summer)
    assert s011._expected_last_closed_trading_date(now) == "2026-09-09"
