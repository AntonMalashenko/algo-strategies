"""Integration check for the risk-sizing wiring in bot.s007_paper.live() --
exercises the real `decide()` closure end-to-end with a fake broker (no
network, no ctrader_open_api SDK needed) so a wiring bug (wrong kwarg order,
stale variable name, ...) is caught here instead of on the live account.
"""
from __future__ import annotations

import sys
import types

import shutil

import pandas as pd
import pytest


@pytest.fixture(autouse=True)
def _clean_position_log():
    """bot.s007_paper.LOG is a module-level singleton writing to the real
    reports/logs/S007/positions/ dir -- without this, a label reused across
    tests (or a real day's leftover logs) would make label_was_closed() (Fix
    1) see a stale 'close' from a PREVIOUS test/run and wrongly skip it in a
    later one. Clear before/after so each test's log state is its own."""
    from bot import s007_paper
    pos_dir = s007_paper.LOG.pos_dir
    shutil.rmtree(pos_dir, ignore_errors=True)
    pos_dir.mkdir(parents=True, exist_ok=True)
    yield
    shutil.rmtree(pos_dir, ignore_errors=True)


class _FakeCTraderS007:
    """Stands in for bot.ctrader_s007.CTraderS007: run_live_cycle calls
    `decide` directly with fixed balance/money_per_point_per_lot, like the
    real broker session would after fetching them once per cycle."""

    last_decide_args = None  # captured for assertions

    def __init__(self, *a, **kw):
        pass

    def run_live_cycle(self, symbol_candidates, history_days, decide):
        m1 = pd.DataFrame(
            {"open": [1.0], "high": [1.0], "low": [1.0], "close": [1.0]},
            index=pd.to_datetime(["2024-05-10 10:05"]),
        )
        balance = 10_000.0
        money_per_point_per_lot = 114.3
        actions = decide("GER40", m1, [], balance, money_per_point_per_lot)
        _FakeCTraderS007.last_decide_args = (balance, money_per_point_per_lot, actions)
        results = [dict(action=a, result={"ok": True}, error=None) for a in actions]
        return dict(symbol="GER40", m1=m1, positions=[], actions=actions,
                    results=results, balance=balance,
                    money_per_point_per_lot=money_per_point_per_lot)


@pytest.fixture
def fake_broker(monkeypatch):
    fake_mod = types.SimpleNamespace(CTraderS007=_FakeCTraderS007)
    monkeypatch.setitem(sys.modules, "bot.ctrader_s007", fake_mod)
    _FakeCTraderS007.last_decide_args = None
    yield _FakeCTraderS007


def test_live_sizes_new_positions_by_risk_not_fixed_lot(fake_broker, monkeypatch):
    from bot import s007_paper, s007_config as C

    monkeypatch.setattr(C, "USE_FIXED_LOT", False)
    monkeypatch.setattr(C, "RISK_PCT", 0.25)
    monkeypatch.setattr(C, "FIXED_LOT", 0.01)

    fake_positions = [
        dict(label="S007:2024-05-10:0", side="buy", entry=18000.0, sl=17950.0,
             tp=18100.0, is_add=False),  # 50-pt stop -> 25/(50*114.3) < min -> 0.01
        dict(label="S007:2024-05-10:1", side="buy", entry=18000.0, sl=17995.0,
             tp=18100.0, is_add=True),   # 5-pt stop -> 25/(5*114.3) ≈ 0.0437 -> sizes UP
    ]
    monkeypatch.setattr(s007_paper, "plan_now", lambda m1: dict(
        in_window=True, day_done=False, flat=False, positions=fake_positions,
        direction="up", context={}))

    s007_paper.live()

    balance, ppp, actions = fake_broker.last_decide_args
    assert balance == 10_000.0 and ppp == 114.3
    by_label = {a["label"]: a for a in actions}
    assert by_label["S007:2024-05-10:0"]["volume_lots"] == pytest.approx(0.01)
    assert by_label["S007:2024-05-10:1"]["volume_lots"] == pytest.approx(25.0 / (5.0 * 114.3))
    assert by_label["S007:2024-05-10:1"]["volume_lots"] > 0.01


def test_live_skips_reopening_a_label_the_log_already_closed(fake_broker, monkeypatch):
    # Fix 1 wiring: the broker's reconcile shows nothing open for this label
    # (broker_positions=[] in _FakeCTraderS007), which is exactly the
    # ambiguous case from the 2026-07-21 bug -- "not open" could mean "never
    # opened" OR "just stopped out, M1 bar hasn't caught up yet". Pre-seed
    # the real per-position log with a recorded close for this label and
    # confirm live() does NOT emit a place action for it, while a sibling
    # label with no such history still gets placed normally.
    from bot import s007_paper, s007_config as C

    monkeypatch.setattr(C, "USE_FIXED_LOT", True)
    monkeypatch.setattr(C, "FIXED_LOT", 0.01)

    already_closed_label = "S007:2024-05-10:0"
    fresh_label = "S007:2024-05-10:1"
    s007_paper.LOG.position(already_closed_label, "open", side="buy", entry=18000.0,
                            sl=17950.0, tp=18100.0, is_add=False)
    s007_paper.LOG.position(already_closed_label, "close", reason="stop")

    fake_positions = [
        dict(label=already_closed_label, side="buy", entry=18000.0, sl=17950.0,
             tp=18100.0, is_add=False),
        dict(label=fresh_label, side="buy", entry=18000.0, sl=17995.0,
             tp=18100.0, is_add=True),
    ]
    monkeypatch.setattr(s007_paper, "plan_now", lambda m1: dict(
        in_window=True, day_done=False, flat=False, positions=fake_positions,
        direction="up", context={}))

    s007_paper.live()

    _, _, actions = fake_broker.last_decide_args
    labels = {a["label"] for a in actions}
    assert already_closed_label not in labels
    assert fresh_label in labels


def test_live_uses_fixed_lot_when_flag_set(fake_broker, monkeypatch):
    from bot import s007_paper, s007_config as C

    monkeypatch.setattr(C, "USE_FIXED_LOT", True)
    monkeypatch.setattr(C, "FIXED_LOT", 0.01)

    fake_positions = [
        dict(label="S007:2024-05-10:0", side="buy", entry=18000.0, sl=17950.0,
             tp=18100.0, is_add=False),
    ]
    monkeypatch.setattr(s007_paper, "plan_now", lambda m1: dict(
        in_window=True, day_done=False, flat=False, positions=fake_positions,
        direction="up", context={}))

    s007_paper.live()

    _, _, actions = fake_broker.last_decide_args
    assert actions[0]["volume_lots"] == 0.01
