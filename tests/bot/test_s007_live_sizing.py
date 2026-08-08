"""Integration check for the risk-sizing wiring in bot.s007_paper.live() --
exercises the real `decide()` closure end-to-end with a fake broker (no
network, no ctrader_open_api SDK needed) so a wiring bug (wrong kwarg order,
stale variable name, ...) is caught here instead of on the live account.
"""
from __future__ import annotations

import sys
import types

import pandas as pd
import pytest


@pytest.fixture(autouse=True)
def _clean_position_log(monkeypatch, tmp_path):
    """bot.s007_paper.LOG is a module-level singleton; unpatched, it writes to
    the REAL reports/logs/S007/ directory. This used to be "cleaned" with
    shutil.rmtree() on that real positions/ dir before/after each test --
    which is exactly as destructive as it sounds: running this file while the
    live launchd tick (com.anton.algo.s007bot) happened to be mid-cycle
    deleted the production per-position history (everything before that day)
    and made a live cycle throw FileNotFoundError trying to write into the
    now-missing directory (found live 2026-08-06, see decisions-log.md; no
    data was actually lost only because every position/order event is ALSO
    duplicated into reports/logs/S007/events-<date>.jsonl, which this never
    touched). Point LOG at a throwaway StrategyLogger under tmp_path instead
    -- same "each test's log state is its own" guarantee (a label reused
    across tests won't see a stale 'close' from a previous run), with zero
    risk to anything real."""
    from bot import s007_paper
    from utils.trade_logger import StrategyLogger
    monkeypatch.setattr(s007_paper, "LOG", StrategyLogger("S007TEST", log_root=str(tmp_path),
                                                           console=False))
    yield


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

    # decide() multiplies the raw 114.3 by C.EUR_TO_USD_FX_RATE_APPROX (see
    # bot/s007_config.py) before sizing, so the lots below are computed against
    # 114.3 * C.EUR_TO_USD_FX_RATE_APPROX (~130.66 at the current 1.1427 rate),
    # not the raw 114.3 the fake broker hands in.
    fake_positions = [
        dict(label="S007:2024-05-10:0", side="buy", entry=18000.0, sl=17950.0,
             tp=18100.0, is_add=False),  # 50-pt stop -> still < min -> 0.01
        dict(label="S007:2024-05-10:1", side="buy", entry=18000.0, sl=17995.0,
             tp=18100.0, is_add=True),   # 5-pt stop -> still sizes UP above 0.01
    ]
    monkeypatch.setattr(s007_paper, "plan_now", lambda m1, preset=None: dict(
        in_window=True, day_done=False, flat=False, positions=fake_positions,
        direction="up", context={}))

    s007_paper.live()

    balance, ppp, actions = fake_broker.last_decide_args
    assert balance == 10_000.0 and ppp == 114.3
    by_label = {a["label"]: a for a in actions}
    assert by_label["S007:2024-05-10:0"]["volume_lots"] == pytest.approx(0.01)
    assert by_label["S007:2024-05-10:1"]["volume_lots"] == pytest.approx(
        25.0 / (5.0 * 114.3 * C.EUR_TO_USD_FX_RATE_APPROX))
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
    monkeypatch.setattr(s007_paper, "plan_now", lambda m1, preset=None: dict(
        in_window=True, day_done=False, flat=False, positions=fake_positions,
        direction="up", context={}))

    s007_paper.live()

    _, _, actions = fake_broker.last_decide_args
    labels = {a["label"] for a in actions}
    assert already_closed_label not in labels
    assert fresh_label in labels


def test_live_backfills_close_and_skips_reopen_on_broker_side_stop_before_log_catches_up(
        fake_broker, monkeypatch):
    # 2026-07-30 live incident: broker_positions comes back empty (real stop
    # already filled), but the M1 bar plan_now() used to build `positions`
    # hadn't caught up yet, so it still "wanted" the same label open -- and
    # our own log had an "open" record but no "close" yet (nothing had
    # detected the broker-side close to log it). decide() must recognize
    # this from label_was_opened()+not label_was_closed() alone, backfill the
    # close, and NOT attempt to re-place the position (it did, live, and the
    # broker rejected it with TRADING_BAD_STOPS purely by luck of price
    # having already moved past the stale stop level).
    from bot import s007_paper, s007_config as C

    monkeypatch.setattr(C, "USE_FIXED_LOT", True)
    monkeypatch.setattr(C, "FIXED_LOT", 0.01)

    label = "S007:2026-07-30:3"
    s007_paper.LOG.position(label, "open", side="buy", entry=25388.2,
                            sl=25318.2, tp=25543.4, is_add=False)
    assert s007_paper.LOG.label_was_closed(label) is False  # not backfilled yet

    fake_positions = [
        dict(label=label, side="buy", entry=25388.2, sl=25318.2, tp=25543.4, is_add=False),
    ]
    monkeypatch.setattr(s007_paper, "plan_now", lambda m1, preset=None: dict(
        in_window=True, day_done=False, flat=False, positions=fake_positions,
        direction="up", context={}))

    s007_paper.live()

    _, _, actions = fake_broker.last_decide_args
    assert actions == []
    assert s007_paper.LOG.label_was_closed(label) is True  # backfilled by decide()


def test_live_uses_fixed_lot_when_flag_set(fake_broker, monkeypatch):
    from bot import s007_paper, s007_config as C

    monkeypatch.setattr(C, "USE_FIXED_LOT", True)
    monkeypatch.setattr(C, "FIXED_LOT", 0.01)

    fake_positions = [
        dict(label="S007:2024-05-10:0", side="buy", entry=18000.0, sl=17950.0,
             tp=18100.0, is_add=False),
    ]
    monkeypatch.setattr(s007_paper, "plan_now", lambda m1, preset=None: dict(
        in_window=True, day_done=False, flat=False, positions=fake_positions,
        direction="up", context={}))

    s007_paper.live()

    _, _, actions = fake_broker.last_decide_args
    assert actions[0]["volume_lots"] == 0.01
