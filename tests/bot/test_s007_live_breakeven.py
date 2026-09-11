"""ALGODEV-37 live breakeven: decide() must emit an "amend" action (move the
broker stop to the ACTUAL fill price) for every open position whose engine
be_moved flag fired, exactly once, without ever touching the day-cap
accounting (the frozen 2026-09-02 risk-cap invariant).

Same fake-broker harness as tests/bot/test_s007_live_sizing.py -- exercises
the real decide() closure end-to-end, no network/SDK.
"""
from __future__ import annotations

import sys
import types

import pandas as pd
import pytest


@pytest.fixture(autouse=True)
def _clean_position_log(monkeypatch, tmp_path):
    # Same rationale as test_s007_live_sizing.py: never point the module
    # singleton at the real reports/logs/S007/ tree from a test.
    from bot import s007_paper
    from utils.trade_logger import StrategyLogger
    monkeypatch.setattr(s007_paper, "LOG", StrategyLogger("S007TEST", log_root=str(tmp_path),
                                                          console=False))
    yield


class _FakeWithOpen:
    """Minimal run_live_cycle stand-in handing decide() caller-set open
    broker positions (breakeven only ever acts on already-open positions)."""
    broker_positions: list[dict] = []
    last_decide_args = None

    def __init__(self, *a, **kw):
        pass

    def run_live_cycle(self, symbol_candidates, history_days, decide):
        m1 = pd.DataFrame(
            {"open": [1.0], "high": [1.0], "low": [1.0], "close": [1.0]},
            index=pd.to_datetime(["2024-05-10 10:05"]),
        )
        balance = 10_000.0
        money_per_point_per_lot = 114.3
        actions = decide("GER40", m1, self.broker_positions, balance, money_per_point_per_lot)
        type(self).last_decide_args = (balance, money_per_point_per_lot, actions)
        results = [dict(action=a, result={"ok": True}, error=None) for a in actions]
        return dict(symbol="GER40", m1=m1, positions=self.broker_positions, actions=actions,
                    results=results, balance=balance,
                    money_per_point_per_lot=money_per_point_per_lot)


def _install(monkeypatch, broker_positions, wanted):
    from bot import s007_paper
    fake_cls = type("Fake", (_FakeWithOpen,),
                    {"broker_positions": broker_positions, "last_decide_args": None})
    monkeypatch.setitem(sys.modules, "bot.ctrader_s007",
                        types.SimpleNamespace(CTraderS007=fake_cls))
    monkeypatch.setattr(s007_paper, "plan_now", lambda m1, preset=None: dict(
        in_window=True, day_done=False, flat=False, positions=wanted,
        direction="up", context={}))
    return fake_cls


def _want(label, side="buy", entry=18000.0, sl=18000.0, be_moved=True, **kw):
    return dict(label=label, side=side, entry=entry, sl=sl, tp=18100.0,
                is_add=False, be_moved=be_moved, **kw)


def test_amend_emitted_for_be_moved_position_at_broker_fill_price(monkeypatch):
    """Engine says breakeven fired (be_moved=True, engine sl==entry 18000);
    the broker actually filled at 18002.5 (slippage) with the original stop
    still at 17950 -- the amend must target the broker FILL (18002.5), not
    the engine entry, and must carry the position's current TP through."""
    from bot import s007_paper

    broker = [dict(label="S007:2024-05-10:0", position_id=11, volume=100,
                   price=18002.5, stop_loss=17950.0, take_profit=18100.0, side="buy")]
    fake = _install(monkeypatch, broker, [_want("S007:2024-05-10:0")])

    s007_paper.live()

    _, _, actions = fake.last_decide_args
    amends = [a for a in actions if a["kind"] == "amend"]
    assert len(amends) == 1
    a = amends[0]
    assert a["position_id"] == 11
    assert a["sl"] == 18002.5          # broker fill, NOT engine entry 18000.0
    assert a["tp"] == 18100.0          # current TP preserved
    assert a["prev_sl"] == 17950.0


def test_no_amend_when_be_not_fired_or_stop_already_at_breakeven(monkeypatch):
    """Three open positions: BE not fired -> no amend; BE fired but broker SL
    already at fill (previous cycle's amend succeeded) -> no amend (one-shot
    without extra state); BE fired on a SELL with SL already tightened BEYOND
    breakeven -> never loosened back."""
    from bot import s007_paper

    broker = [
        dict(label="S007:2024-05-10:0", position_id=1, volume=100,
             price=18000.0, stop_loss=17950.0, take_profit=18100.0, side="buy"),
        dict(label="S007:2024-05-10:1", position_id=2, volume=100,
             price=18010.0, stop_loss=18010.0, take_profit=18100.0, side="buy"),
        dict(label="S007:2024-05-10:2", position_id=3, volume=100,
             price=17990.0, stop_loss=17985.0, take_profit=17900.0, side="sell"),
    ]
    wanted = [
        _want("S007:2024-05-10:0", be_moved=False),
        _want("S007:2024-05-10:1", entry=18010.0, sl=18010.0),
        _want("S007:2024-05-10:2", side="sell", entry=17990.0, sl=17990.0),
    ]
    fake = _install(monkeypatch, broker, wanted)

    s007_paper.live()

    _, _, actions = fake.last_decide_args
    assert [a for a in actions if a["kind"] == "amend"] == []


def test_amend_short_side_targets_fill_and_tightens_only(monkeypatch):
    from bot import s007_paper

    broker = [dict(label="S007:2024-05-10:0", position_id=7, volume=100,
                   price=17997.0, stop_loss=18050.0, take_profit=17900.0, side="sell")]
    fake = _install(monkeypatch, broker,
                    [_want("S007:2024-05-10:0", side="sell", entry=18000.0, sl=18000.0)])

    s007_paper.live()

    _, _, actions = fake.last_decide_args
    amends = [a for a in actions if a["kind"] == "amend"]
    assert len(amends) == 1
    assert amends[0]["sl"] == 17997.0   # sell breakeven = fill price (below old SL)
    assert amends[0]["tp"] == 17900.0


class _FakeWithRealBars:
    """Like _FakeWithOpen, but hands decide() real multi-bar M1 data (instead
    of a trivial 1-row placeholder) so the ALGODEV-39 real-fill breakeven
    check has actual highs/lows to compare against."""
    broker_positions: list[dict] = []
    wanted: list[dict] = []
    breakeven_at_r: float | None = 0.5
    bars: pd.DataFrame | None = None
    last_decide_args = None

    def __init__(self, *a, **kw):
        pass

    def run_live_cycle(self, symbol_candidates, history_days, decide):
        balance = 10_000.0
        money_per_point_per_lot = 114.3
        actions = decide("GER40", type(self).bars, self.broker_positions, balance,
                         money_per_point_per_lot)
        type(self).last_decide_args = (balance, money_per_point_per_lot, actions)
        results = [dict(action=a, result={"ok": True}, error=None) for a in actions]
        return dict(symbol="GER40", m1=type(self).bars, positions=self.broker_positions,
                    actions=actions, results=results, balance=balance,
                    money_per_point_per_lot=money_per_point_per_lot)


def _install_with_real_bars(monkeypatch, broker_positions, wanted, bars, breakeven_at_r=0.5):
    from bot import s007_paper
    fake_cls = type("FakeWithRealBars", (_FakeWithRealBars,), {
        "broker_positions": broker_positions, "wanted": wanted, "bars": bars,
        "breakeven_at_r": breakeven_at_r, "last_decide_args": None,
    })
    monkeypatch.setitem(sys.modules, "bot.ctrader_s007",
                        types.SimpleNamespace(CTraderS007=fake_cls))
    monkeypatch.setattr(s007_paper, "plan_now", lambda m1, preset=None: dict(
        in_window=True, day_done=False, flat=False, positions=wanted,
        direction="up", context={}, breakeven_at_r=breakeven_at_r))
    return fake_cls


def test_amend_fires_off_real_fill_when_engine_be_moved_is_still_false(monkeypatch):
    """ALGODEV-39, found live 2026-09-04: a slipped fill can put the REAL
    price 0.5R+ of its REAL (fill-to-stop) risk in favor while the engine
    -- replaying its own clean theoretical entry -- still reports far less
    than 0.5R and never sets be_moved. The amend must still fire, using the
    real fill/stop already available in `have`."""
    from bot import s007_paper

    # Real fill 18000.0, real stop 17950.0 -> real risk 50.0, 0.5R trigger
    # at 18025.0. The engine's own (unrelated, wider) entry/risk means
    # be_moved is still False -- this must trigger anyway.
    broker = [dict(label="S007:2024-05-10:0", position_id=21, volume=100,
                   price=18000.0, stop_loss=17950.0, take_profit=18100.0, side="buy",
                   opened_ts=int(pd.Timestamp("2024-05-10 10:00", tz="Europe/Bucharest")
                                .tz_convert("UTC").timestamp() * 1000))]
    wanted = [dict(label="S007:2024-05-10:0", side="buy", entry=17990.0, sl=17900.0,
                   tp=18100.0, is_add=False, be_moved=False)]
    bars = pd.DataFrame(
        {"open": [18000.0, 18010.0], "high": [18010.0, 18030.0],
         "low": [17995.0, 18005.0], "close": [18008.0, 18020.0]},
        index=pd.to_datetime(["2024-05-10 10:05", "2024-05-10 10:06"]),
    )
    fake = _install_with_real_bars(monkeypatch, broker, wanted, bars)

    s007_paper.live()

    _, _, actions = fake.last_decide_args
    amends = [a for a in actions if a["kind"] == "amend"]
    assert len(amends) == 1
    assert amends[0]["sl"] == 18000.0   # moved to the real fill
    assert amends[0]["position_id"] == 21


def test_no_amend_when_real_price_has_not_reached_real_breakeven_yet(monkeypatch):
    """Same setup as above, but the bars never actually reach the real 0.5R
    trigger (18025.0) -- must NOT amend just because breakeven_at_r is set."""
    from bot import s007_paper

    broker = [dict(label="S007:2024-05-10:0", position_id=21, volume=100,
                   price=18000.0, stop_loss=17950.0, take_profit=18100.0, side="buy",
                   opened_ts=int(pd.Timestamp("2024-05-10 10:00", tz="Europe/Bucharest")
                                .tz_convert("UTC").timestamp() * 1000))]
    wanted = [dict(label="S007:2024-05-10:0", side="buy", entry=17990.0, sl=17900.0,
                   tp=18100.0, is_add=False, be_moved=False)]
    bars = pd.DataFrame(
        {"open": [18000.0, 18005.0], "high": [18008.0, 18012.0],
         "low": [17995.0, 18000.0], "close": [18003.0, 18008.0]},
        index=pd.to_datetime(["2024-05-10 10:05", "2024-05-10 10:06"]),
    )
    fake = _install_with_real_bars(monkeypatch, broker, wanted, bars)

    s007_paper.live()

    _, _, actions = fake.last_decide_args
    assert [a for a in actions if a["kind"] == "amend"] == []


def test_amend_target_carries_the_breakeven_offset_past_the_fill(monkeypatch):
    """ALGODEV-41: with breakeven_offset_points set, the amend must target
    fill + offset for a buy (fill - offset for a sell), so a retrace onto the
    BE stop clears the round-trip spread instead of booking it as a loss.
    Market is far away here, so nothing clamps."""
    from bot import s007_paper

    broker = [dict(label="S007:2024-05-10:0", position_id=31, volume=100,
                   price=18000.0, stop_loss=17950.0, take_profit=18100.0, side="buy",
                   opened_ts=int(pd.Timestamp("2024-05-10 10:00", tz="Europe/Bucharest")
                                .tz_convert("UTC").timestamp() * 1000))]
    wanted = [dict(label="S007:2024-05-10:0", side="buy", entry=18000.0, sl=18000.0,
                   tp=18100.0, is_add=False, be_moved=True)]
    bars = pd.DataFrame(
        {"open": [18040.0], "high": [18050.0], "low": [18035.0], "close": [18045.0]},
        index=pd.to_datetime(["2024-05-10 10:06"]),
    )
    fake = _install_with_real_bars(monkeypatch, broker, wanted, bars)
    monkeypatch.setattr(s007_paper, "plan_now", lambda m1, preset=None: dict(
        in_window=True, day_done=False, flat=False, positions=wanted,
        direction="up", context={}, breakeven_at_r=0.5, breakeven_offset_points=3.0))

    s007_paper.live()

    _, _, actions = fake.last_decide_args
    amends = [a for a in actions if a["kind"] == "amend"]
    assert len(amends) == 1
    assert amends[0]["sl"] == 18003.0       # fill 18000 + 3pt offset, not the bare fill


def test_amend_offset_is_clamped_to_the_safe_side_of_the_latest_bar(monkeypatch):
    """ALGODEV-41: if price has ALREADY retraced to near fill+offset, an amend
    at fill+offset would be a stop on the wrong side of the market -- exactly
    the TRADING_BAD_STOPS rejection seen live. The target is capped at the
    last bar's low less the named buffer, and never falls back past `fill`."""
    from bot import s007_paper

    broker = [dict(label="S007:2024-05-10:0", position_id=32, volume=100,
                   price=18000.0, stop_loss=17950.0, take_profit=18100.0, side="buy",
                   opened_ts=int(pd.Timestamp("2024-05-10 10:00", tz="Europe/Bucharest")
                                .tz_convert("UTC").timestamp() * 1000))]
    wanted = [dict(label="S007:2024-05-10:0", side="buy", entry=18000.0, sl=18000.0,
                   tp=18100.0, is_add=False, be_moved=True)]
    # last bar's low is 18002 -> cap = 18002 - 1.5 = 18000.5, well short of
    # the requested 18000 + 3 = 18003.
    bars = pd.DataFrame(
        {"open": [18003.0], "high": [18004.0], "low": [18002.0], "close": [18002.5]},
        index=pd.to_datetime(["2024-05-10 10:06"]),
    )
    fake = _install_with_real_bars(monkeypatch, broker, wanted, bars)
    monkeypatch.setattr(s007_paper, "plan_now", lambda m1, preset=None: dict(
        in_window=True, day_done=False, flat=False, positions=wanted,
        direction="up", context={}, breakeven_at_r=0.5, breakeven_offset_points=3.0))

    s007_paper.live()

    _, _, actions = fake.last_decide_args
    amends = [a for a in actions if a["kind"] == "amend"]
    assert len(amends) == 1
    assert amends[0]["sl"] == 18002.0 - s007_paper.BREAKEVEN_AMEND_MARKET_BUFFER_POINTS
    assert amends[0]["sl"] >= 18000.0       # never worse than the old sl=fill behaviour


def test_breakeven_stop_price_falls_back_to_fill_on_a_deep_retrace(monkeypatch):
    """The clamp is one-sided: a retrace already BELOW fill can never push the
    BE stop below the fill (that would be looser than the frozen ALGODEV-37
    behaviour), it just degrades to the plain sl=fill amend."""
    from bot import s007_paper

    bars = pd.DataFrame(
        {"open": [17999.0], "high": [18001.0], "low": [17999.0], "close": [18000.0]},
        index=pd.to_datetime(["2024-05-10 10:06"]),
    )
    assert s007_paper.breakeven_stop_price("buy", 18000.0, 3.0, bars) == 18000.0
    assert s007_paper.breakeven_stop_price("sell", 18000.0, 3.0, bars) == 18000.0
    # offset 0.0 (every preset that doesn't opt in) is the identity case
    assert s007_paper.breakeven_stop_price("buy", 18000.0, 0.0, bars) == 18000.0
    assert s007_paper.breakeven_stop_price("sell", 18000.0, 0.0, bars) == 18000.0


def test_amend_does_not_consume_day_caps(monkeypatch):
    """Frozen invariant (2026-09-02 risk-cap fix): an amend opens nothing.
    With the day already AT the count cap (4 open of 4 allowed at
    2%/0.5%), a BE amend must still go through, and a 5th new position must
    still be blocked -- i.e. the amend neither consumes budget nor unlocks
    any (the count cap keeps counting the BE'd position)."""
    from bot import s007_paper, s007_config as C

    monkeypatch.setattr(C, "USE_FIXED_LOT", True)
    monkeypatch.setattr(C, "FIXED_LOT", 0.01)
    monkeypatch.setattr(C, "RISK_PCT", 0.5)
    monkeypatch.setattr(C, "DAILY_RISK_CAP_PCT", 2.0)   # -> 4 positions/day

    broker = [
        dict(label=f"S007:2024-05-10:{i}", position_id=i, volume=100,
             price=18000.0 + i, stop_loss=17998.0 + i, take_profit=18100.0, side="buy")
        for i in range(4)
    ]
    wanted = [
        # position :0 crossed breakeven per the engine
        _want("S007:2024-05-10:0", entry=18000.0, sl=18000.0),
        *[_want(f"S007:2024-05-10:{i}", entry=18000.0 + i, sl=17998.0 + i, be_moved=False)
          for i in range(1, 4)],
        # a 5th, new position the engine wants -- must be count-capped
        _want("S007:2024-05-10:4", entry=18004.0, sl=18002.0, be_moved=False),
    ]
    fake = _install(monkeypatch, broker, wanted)

    s007_paper.live()

    _, _, actions = fake.last_decide_args
    kinds = {a["label"]: a["kind"] for a in actions}
    assert kinds == {"S007:2024-05-10:0": "amend"}   # amend passed, 5th blocked
