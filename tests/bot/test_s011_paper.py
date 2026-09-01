"""ALGODEV-34: bot/s011_paper.py::run_cycle_for_account must not record
prev_held/position_value as if a broker action succeeded when it didn't.

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


def _bars(n=3):
    idx = pd.date_range("2026-01-01", periods=n, freq="D")
    return pd.DataFrame({"close": [100.0] * n, "high": [101.0] * n, "low": [99.0] * n,
                         "open": [100.0] * n}, index=idx)


class _FakeClient:
    """Stands in for bot.ctrader_s011.CTraderS011 -- run_live_cycle_multi
    calls `decide` once with fixture daily_bars/positions/resolved, then
    reports each returned broker_action as succeeded or failed per the
    test-controlled `fail_assets` set (mirroring what a real MARKET_CLOSED
    rejection looks like from run_cycle_for_account's point of view)."""

    def __init__(self, held_by_asset, existing_positions, resolved, fail_assets, **kwargs):
        self._held = held_by_asset
        self._positions = existing_positions
        self._resolved = resolved
        self._fail_assets = fail_assets

    def run_live_cycle_multi(self, candidates_by_asset, history_days, decide):
        daily_bars = {a: _bars() for a in self._held}
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
         fail_assets, prev_held, position_value, cash):
    """Wires a fake CTraderS011 + a per-asset RSI stub, then runs one cycle."""
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
        CTraderS011=lambda **kw: _FakeClient(held_by_asset, existing_positions, resolved, fail_assets))
    monkeypatch.setitem(sys.modules, "bot.ctrader_s011", fake_mod)

    state = FileStateStore(tmp_path / "state.json", default_factory=s011._default_state)
    state.save({"last_date": "2026-08-31", "cash": cash, "equity": cash + sum(position_value.values()),
               "position_value": position_value, "prev_held": prev_held})

    cfg = _cfg()
    result = s011.run_cycle_for_account(
        account_key="acct-a", creds={"api_key": "k"}, cfg=cfg, state=state,
        logger=_log(tmp_path), broker="execute", allow_mainnet=False,
        candidates={a: (a,) for a in held_by_asset})
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
    def fake_rsi2_signal(bars, cfg):
        return pd.Series([1], index=bars.index[-1:])
    monkeypatch.setattr(s011, "rsi2_signal", fake_rsi2_signal)
    fake_mod = types.SimpleNamespace(
        CTraderS011=lambda **kw: _FakeClient({"DOW": 1}, [], {"DOW": "US30"}, set()))
    monkeypatch.setitem(sys.modules, "bot.ctrader_s011", fake_mod)

    state = FileStateStore(tmp_path / "state.json", default_factory=s011._default_state)
    result2 = s011.run_cycle_for_account(
        account_key="acct-a", creds={"api_key": "k"}, cfg=_cfg(), state=state,
        logger=_log(tmp_path), broker="execute", allow_mainnet=False,
        candidates={"DOW": ("DOW",)})

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
