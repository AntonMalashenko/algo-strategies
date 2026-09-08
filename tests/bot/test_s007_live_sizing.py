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


def test_fresh_placement_uses_orig_sl_not_an_already_be_moved_sl(fake_broker, monkeypatch):
    """ALGODEV-38, found live 2026-09-03: a label seen for the FIRST time
    (never open at the broker before) can already come back from plan_now()
    with be_moved=True / sl == entry, if the engine's bar-replay determined
    breakeven had already triggered before this cycle ever ran. Placing the
    order with that already-moved sl gives a zero-distance stop -- an
    instant stop-out on the next tick/spread, and (worse) a $0 nominal risk
    that can silently bypass the day's $ risk cap (new_risk = lot *
    stop_distance * ... = 0). decide() must use orig_sl (the pre-breakeven
    stop) for a brand-new placement instead."""
    from bot import s007_paper, s007_config as C

    monkeypatch.setattr(C, "USE_FIXED_LOT", True)
    monkeypatch.setattr(C, "FIXED_LOT", 0.01)

    fake_positions = [
        # sl == entry (already "be_moved" by the engine's replay), but
        # orig_sl carries the real, wide, pre-breakeven stop.
        dict(label="S007:2024-05-10:0", side="buy", entry=18000.0, sl=18000.0,
             orig_sl=17950.0, tp=18100.0, is_add=False, be_moved=True),
    ]
    monkeypatch.setattr(s007_paper, "plan_now", lambda m1, preset=None: dict(
        in_window=True, day_done=False, flat=False, positions=fake_positions,
        direction="up", context={}))

    s007_paper.live()

    _, _, actions = fake_broker.last_decide_args
    placed = [a for a in actions if a["label"] == "S007:2024-05-10:0"]
    assert len(placed) == 1
    assert placed[0]["sl"] == pytest.approx(17950.0)   # orig_sl, NOT entry


class _FakeCTraderS007Slipped:
    """run_live_cycle returns a `post_positions` reconcile whose real fill
    price/stop differ from what decide() requested -- simulates broker-side
    slippage on a fresh market order (ALGODEV-39)."""

    last_decide_args = None
    real_price = 18025.0
    real_stop = 17953.5

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
        type(self).last_decide_args = (balance, money_per_point_per_lot, actions)
        results = []
        post_positions = []
        for a in actions:
            if a["kind"] == "place":
                res = types.SimpleNamespace(position=types.SimpleNamespace(positionId=777))
                results.append(dict(action=a, result=res, error=None))
                post_positions.append(dict(
                    position_id=777, label=a["label"], side=a["side"],
                    volume=int(a["volume_lots"] * 100),
                    price=type(self).real_price, stop_loss=type(self).real_stop,
                    take_profit=a["tp"]))
            else:
                results.append(dict(action=a, result={"ok": True}, error=None))
        return dict(symbol="GER40", m1=m1, positions=[], post_positions=post_positions,
                    actions=actions, results=results, balance=balance,
                    money_per_point_per_lot=money_per_point_per_lot)


def test_fresh_placement_logs_the_real_broker_fill_not_the_planned_entry(monkeypatch):
    """ALGODEV-39 follow-up, found live 2026-09-04: our own position log/DB
    used to always record the ENGINE's planned entry/sl (a['entry']/a['sl']),
    never the broker's real fill -- a slipped short's real entry differed
    from the logged one by 25pts, throwing off downstream breakeven/risk math
    relative to what the trader's real account actually shows. run_live_cycle
    now does one extra reconcile right after placing (`post_positions`); the
    caller must prefer those real values over the planned ones when logging
    the open and building actions_taken (which feeds the DB Position row)."""
    from bot import s007_paper, s007_config as C

    fake_mod = types.SimpleNamespace(CTraderS007=_FakeCTraderS007Slipped)
    monkeypatch.setitem(sys.modules, "bot.ctrader_s007", fake_mod)
    monkeypatch.setattr(C, "USE_FIXED_LOT", True)
    monkeypatch.setattr(C, "FIXED_LOT", 0.01)

    fake_positions = [
        dict(label="S007:2024-05-10:0", side="sell", entry=18000.0, sl=18050.0,
             tp=17900.0, is_add=False),
    ]
    monkeypatch.setattr(s007_paper, "plan_now", lambda m1, preset=None: dict(
        in_window=True, day_done=False, flat=False, positions=fake_positions,
        direction="down", context={}))

    result = s007_paper.run_cycle_for_account(
        None, preset=C.PRESET, risk_pct=C.RISK_PCT, fixed_lot=C.FIXED_LOT,
        use_fixed_lot=C.USE_FIXED_LOT, magic=C.MAGIC, logger=s007_paper.LOG)

    opened = [a for a in result["actions"] if a["kind"] == "open"]
    assert len(opened) == 1
    assert opened[0]["entry"] == pytest.approx(_FakeCTraderS007Slipped.real_price)
    assert opened[0]["sl"] == pytest.approx(_FakeCTraderS007Slipped.real_stop)


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


class _FakeCTraderS007WithOpenPositions(_FakeCTraderS007):
    """Same contract as _FakeCTraderS007, but run_live_cycle hands decide()
    a caller-supplied list of already-open broker positions instead of []
    -- needed to test the day-level position cap, which counts already-open
    positions (`have`) against the config-derived max, not just what a
    single cycle wants to newly place."""
    broker_positions: list[dict] = []

    def run_live_cycle(self, symbol_candidates, history_days, decide):
        m1 = pd.DataFrame(
            {"open": [1.0], "high": [1.0], "low": [1.0], "close": [1.0]},
            index=pd.to_datetime(["2024-05-10 10:05"]),
        )
        balance = 10_000.0
        money_per_point_per_lot = 114.3
        actions = decide("GER40", m1, self.broker_positions, balance, money_per_point_per_lot)
        _FakeCTraderS007.last_decide_args = (balance, money_per_point_per_lot, actions)
        results = [dict(action=a, result={"ok": True}, error=None) for a in actions]
        return dict(symbol="GER40", m1=m1, positions=self.broker_positions, actions=actions,
                    results=results, balance=balance,
                    money_per_point_per_lot=money_per_point_per_lot)


def test_live_caps_positions_at_config_derived_count_not_dollar_risk(monkeypatch):
    """ALGODEV-36: max_positions_per_day = floor(daily_risk_cap_pct /
    risk_pct), a plain count against config values, gates INDEPENDENTLY of
    the (also-active, ALGODEV-37) $ risk_cap check. 2% cap / 0.5% per
    position = 4 max/day. 2 already open (from a prior cycle) + 3 newly
    wanted this cycle -- only 2 of the 3 new ones may be placed (2+2=4),
    the 3rd must be skipped regardless of its own stop distance/size. Stop
    distances here are deliberately tiny so the $ risk_cap check (also
    active) never binds first -- this test isolates the COUNT cap; see
    test_live_dollar_risk_cap_uses_initial_balance_not_live_balance below
    for the $ gate itself."""
    from bot import s007_paper, s007_config as C

    monkeypatch.setattr(C, "USE_FIXED_LOT", True)   # sizing irrelevant to the cap itself
    monkeypatch.setattr(C, "FIXED_LOT", 0.01)
    monkeypatch.setattr(C, "RISK_PCT", 0.5)
    monkeypatch.setattr(C, "DAILY_RISK_CAP_PCT", 2.0)   # -> max_positions_per_day = 4

    already_open = [
        dict(label="S007:2024-05-10:0", position_id=1, volume=100, price=18000.0, stop_loss=17998.0),
        dict(label="S007:2024-05-10:1", position_id=2, volume=100, price=18010.0, stop_loss=18008.0),
    ]
    fake_positions = [
        dict(label="S007:2024-05-10:0", side="buy", entry=18000.0, sl=17998.0, tp=18100.0, is_add=False),
        dict(label="S007:2024-05-10:1", side="buy", entry=18000.0, sl=17998.0, tp=18100.0, is_add=True),
        dict(label="S007:2024-05-10:2", side="buy", entry=18010.0, sl=18008.0, tp=18100.0, is_add=True),
        dict(label="S007:2024-05-10:3", side="buy", entry=18020.0, sl=18018.0, tp=18100.0, is_add=True),
        dict(label="S007:2024-05-10:4", side="buy", entry=18030.0, sl=18028.0, tp=18100.0, is_add=True),
    ]
    monkeypatch.setattr(s007_paper, "plan_now", lambda m1, preset=None: dict(
        in_window=True, day_done=False, flat=False, positions=fake_positions,
        direction="up", context={}))

    fake_cls = type("FakeWithOpen", (_FakeCTraderS007WithOpenPositions,),
                    {"broker_positions": already_open})
    fake_mod = types.SimpleNamespace(CTraderS007=fake_cls)
    monkeypatch.setitem(sys.modules, "bot.ctrader_s007", fake_mod)
    _FakeCTraderS007.last_decide_args = None

    s007_paper.live()

    _, _, actions = fake_cls.last_decide_args
    placed_labels = {a["label"] for a in actions}
    # labels :0/:1 are already open (have) -- not re-placed; of the 3 NEW
    # wanted labels (:2/:3/:4), only 2 fit under the cap (2 open + 2 new = 4).
    assert placed_labels == {"S007:2024-05-10:2", "S007:2024-05-10:3"}
    assert "S007:2024-05-10:4" not in placed_labels


def test_live_dollar_risk_cap_uses_initial_balance_not_live_balance(fake_broker, monkeypatch):
    """ALGODEV-37: risk_cap = initial_balance * daily_risk_cap_pct / 100,
    NOT the broker's live balance -- found live 2026-09-02 (Anton): using
    live balance means the cap shrinks right along with the day's already-
    realized losses (2% of an already-reduced balance is a smaller $
    number), which is backwards for a budget meant to bound the day's risk.

    _FakeCTraderS007 (fake_broker fixture) always hands decide() a live
    balance of $10,000 -- passing initial_balance=$20,000 here must double
    the $ cap to $400 (not $200), letting a 3rd $111-ish position through
    that would be skipped under a live-balance-based $200 cap."""
    from bot import s007_paper, s007_config as C

    monkeypatch.setattr(C, "USE_FIXED_LOT", True)
    monkeypatch.setattr(C, "FIXED_LOT", 0.01)
    monkeypatch.setattr(C, "DAILY_RISK_CAP_PCT", 2.0)

    # Each ~85pt-stop position at fixed_lot=0.01 risks ~$111 (0.01 * 85 *
    # 130.61 with the fake broker's 114.3 * EUR_TO_USD_FX_RATE_APPROX) --
    # three of them (~$333 total) fit under a $400 (initial_balance=20000)
    # cap but not a $200 (live balance=10000) one.
    fake_positions = [
        dict(label="S007:2024-05-10:0", side="buy", entry=18000.0, sl=17915.0, tp=18200.0, is_add=False),
        dict(label="S007:2024-05-10:1", side="buy", entry=18010.0, sl=17925.0, tp=18200.0, is_add=True),
        dict(label="S007:2024-05-10:2", side="buy", entry=18020.0, sl=17935.0, tp=18200.0, is_add=True),
    ]
    monkeypatch.setattr(s007_paper, "plan_now", lambda m1, preset=None: dict(
        in_window=True, day_done=False, flat=False, positions=fake_positions,
        direction="up", context={}))

    from bot import s007_config as C2
    result = s007_paper.run_cycle_for_account(
        None, preset=C2.PRESET, risk_pct=C2.RISK_PCT, fixed_lot=C2.FIXED_LOT,
        use_fixed_lot=C2.USE_FIXED_LOT, magic=C2.MAGIC, logger=s007_paper.LOG,
        initial_balance=20_000.0)

    assert result["error"] is None
    _, _, actions = fake_broker.last_decide_args
    placed_labels = {a["label"] for a in actions}
    assert placed_labels == {"S007:2024-05-10:0", "S007:2024-05-10:1", "S007:2024-05-10:2"}


def test_live_day_caps_survive_stop_out_waves(fake_broker, monkeypatch):
    """2026-09-02 live incident: both day caps were computed from the broker's
    open-positions snapshot, so every stop-out wave zeroed them and the next
    wave got a fresh budget -- 8 positions / ~4% lost on a 2% cap day. The
    count cap must run against every label OUR LOG opened today (closed or
    not): 3 opened -> a 4th still places; the moment 4 have opened, a 5th is
    skipped even with the broker snapshot completely empty."""
    from bot import s007_paper, s007_config as C

    monkeypatch.setattr(C, "USE_FIXED_LOT", True)
    monkeypatch.setattr(C, "FIXED_LOT", 0.01)
    monkeypatch.setattr(C, "RISK_PCT", 0.5)
    monkeypatch.setattr(C, "DAILY_RISK_CAP_PCT", 2.0)   # -> max 4 positions/day

    # Wave 1 + 2 already came and went: 3 positions opened and stopped out.
    # broker_positions is [] (nothing open NOW) -- exactly the live shape.
    for idx in (0, 19, 51):
        lab = f"S007:2024-05-10:{idx}"
        s007_paper.LOG.position(lab, "open", side="buy", entry=18000.0,
                                sl=17998.0, tp=18100.0, is_add=False, volume_lots=0.01)
        s007_paper.LOG.position(lab, "close", reason="stop")

    def want(label):
        return [dict(label=label, side="buy", entry=18010.0, sl=18008.0,
                     tp=18100.0, is_add=True)]

    monkeypatch.setattr(s007_paper, "plan_now", lambda m1, preset=None: dict(
        in_window=True, day_done=False, flat=False, positions=want("S007:2024-05-10:63"),
        direction="up", context={}))
    s007_paper.live()
    _, _, actions = fake_broker.last_decide_args
    assert {a["label"] for a in actions} == {"S007:2024-05-10:63"}  # 4th of the day: allowed

    # That placement was logged -> 4 opened today. A 5th must be refused even
    # though the broker still shows nothing open.
    monkeypatch.setattr(s007_paper, "plan_now", lambda m1, preset=None: dict(
        in_window=True, day_done=False, flat=False, positions=want("S007:2024-05-10:80"),
        direction="up", context={}))
    s007_paper.live()
    _, _, actions = fake_broker.last_decide_args
    assert actions == []


class _FakeCTraderS007WithDeals(_FakeCTraderS007):
    """Hands decide() a caller-supplied closed-deals list (what the real
    run_live_cycle fetches from the broker's deal history), with an empty
    open-positions snapshot -- the sub-minute-lifetime case where deal
    history is the only place a fill price still exists."""
    closed_deals: list[dict] = []

    def run_live_cycle(self, symbol_candidates, history_days, decide):
        m1 = pd.DataFrame(
            {"open": [1.0], "high": [1.0], "low": [1.0], "close": [1.0]},
            index=pd.to_datetime(["2024-05-10 10:05"]),
        )
        balance = 10_000.0
        money_per_point_per_lot = 114.3
        actions = decide("GER40", m1, [], balance, money_per_point_per_lot,
                         closed_deals=self.closed_deals)
        _FakeCTraderS007.last_decide_args = (balance, money_per_point_per_lot, actions)
        results = [dict(action=a, result={"ok": True}, error=None) for a in actions]
        return dict(symbol="GER40", m1=m1, positions=[], actions=actions,
                    results=results, balance=balance,
                    money_per_point_per_lot=money_per_point_per_lot)


def test_live_dollar_budget_counts_slipped_fills_from_deal_history(monkeypatch):
    """Slippage case: two closed positions were logged at a 50-pt planned
    stop distance ($57.15 each at lot 0.01 / 114.3 $/pt) but actually FILLED
    88 pt from the stop ($100.58 each) -- only the broker's deal history
    still knows that, matched by the position_id logged at open. Real spent
    today = $201.16 > the $200 cap, so a 3rd position (nominal $57.15, count
    cap not binding at 3 < 4) must be skipped. Planned-entry math alone
    ($114.30 spent) would have let it through -- that's the assertion."""
    from bot import s007_paper, s007_config as C

    monkeypatch.setattr(C, "USE_FIXED_LOT", True)
    monkeypatch.setattr(C, "FIXED_LOT", 0.01)
    monkeypatch.setattr(C, "RISK_PCT", 0.5)
    monkeypatch.setattr(C, "DAILY_RISK_CAP_PCT", 2.0)   # -> $200 on a 10k balance

    closed_deals = []
    for idx, pid in ((0, 111), (19, 222)):
        lab = f"S007:2024-05-10:{idx}"
        s007_paper.LOG.position(lab, "open", side="buy", entry=18000.0,
                                sl=17950.0, tp=18100.0, is_add=False,
                                volume_lots=0.01, position_id=pid)
        s007_paper.LOG.position(lab, "close", reason="stop")
        closed_deals.append(dict(position_id=pid, entry_price=18038.0,  # 88 pt from sl
                                 exit_price=17950.0, closed_volume=100, pnl=-100.58))

    fake_positions = [
        dict(label="S007:2024-05-10:51", side="buy", entry=18010.0, sl=17960.0,
             tp=18100.0, is_add=True),
    ]
    monkeypatch.setattr(s007_paper, "plan_now", lambda m1, preset=None: dict(
        in_window=True, day_done=False, flat=False, positions=fake_positions,
        direction="up", context={}))

    fake_cls = type("FakeWithDeals", (_FakeCTraderS007WithDeals,),
                    {"closed_deals": closed_deals})
    fake_mod = types.SimpleNamespace(CTraderS007=fake_cls)
    monkeypatch.setitem(sys.modules, "bot.ctrader_s007", fake_mod)
    _FakeCTraderS007.last_decide_args = None

    result = s007_paper.run_cycle_for_account(
        None, preset=C.PRESET, risk_pct=C.RISK_PCT, fixed_lot=C.FIXED_LOT,
        use_fixed_lot=C.USE_FIXED_LOT, magic=C.MAGIC, logger=s007_paper.LOG,
        fx_rate=1.0)

    assert result["error"] is None
    _, _, actions = fake_cls.last_decide_args
    assert actions == []  # blocked by the real (slipped) $ spend, not planned

    # Same setup, deal history unavailable (fetch failed / empty): planned-
    # entry fallback gives $114.30 spent -> the 3rd position fits and places.
    fake_cls2 = type("FakeNoDeals", (_FakeCTraderS007WithDeals,), {"closed_deals": []})
    monkeypatch.setitem(sys.modules, "bot.ctrader_s007",
                        types.SimpleNamespace(CTraderS007=fake_cls2))
    result = s007_paper.run_cycle_for_account(
        None, preset=C.PRESET, risk_pct=C.RISK_PCT, fixed_lot=C.FIXED_LOT,
        use_fixed_lot=C.USE_FIXED_LOT, magic=C.MAGIC, logger=s007_paper.LOG,
        fx_rate=1.0)
    assert result["error"] is None
    _, _, actions = fake_cls2.last_decide_args
    assert {a["label"] for a in actions} == {"S007:2024-05-10:51"}


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
