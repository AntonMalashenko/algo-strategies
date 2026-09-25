"""Tests for S011's migration onto the shared cTrader client.

`bot/ctrader_s011.py` no longer speaks the Open API itself -- it composes
`CTraderApiClient`. These tests pin the two things that migration could
silently break: the S011 D1 session-date relabelling now has to be applied on
top of the client's neutral UTC bars, and one rejected instrument must still
not abort the rest of the portfolio cycle.
"""
from datetime import datetime, timezone

import pandas as pd
import pytest

from bot.ctrader_s011 import CTraderS011


def _utc_bars(open_stamps, closes):
    """Bars shaped like CTraderApiClient.get_trendbars: tz-naive UTC index."""
    return pd.DataFrame({"close": closes},
                        index=pd.to_datetime(open_stamps))


class FakeBroker:
    """Stand-in for CTraderApiClient, used as the context manager S011 opens."""

    def __init__(self, *, bars_by_symbol, positions=(), reject_symbols=()):
        self.bars_by_symbol = bars_by_symbol
        self.positions = list(positions)
        self.reject_symbols = set(reject_symbols)
        self.orders = []
        self.closed = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def resolve_symbols(self, candidates_by_key):
        resolved = {}
        for key, candidates in candidates_by_key.items():
            for candidate in candidates:
                if candidate in self.bars_by_symbol:
                    resolved[key] = candidate
                    break
        return resolved

    def get_trendbars_many(self, symbols, period, days):
        assert period == "D1"
        return {symbol: self.bars_by_symbol[symbol] for symbol in symbols}

    def get_balance(self):
        return 10_000.0

    def get_open_positions(self):
        return self.positions

    def get_symbols_details(self, symbols):
        return {symbol.upper(): dict(name=symbol.upper(), lot_size=1) for symbol in symbols}

    def place_market_order(self, symbol, side, **kwargs):
        if symbol in self.reject_symbols:
            raise RuntimeError("cTrader error: MARKET_CLOSED")
        self.orders.append(dict(symbol=symbol, side=side, **kwargs))
        return dict(ok=True)

    def close_position(self, position_id, volume):
        self.closed.append((position_id, volume))
        return dict(ok=True)


def _strategy(broker: FakeBroker) -> CTraderS011:
    strategy = CTraderS011.__new__(CTraderS011)   # skip real client construction
    strategy.client = broker
    return strategy


class TestSessionDateRelabelling:
    def test_evening_open_rolls_to_the_next_day(self):
        """A bar the broker stamps at 21:00Z on the 7th IS the 8th's session."""
        relabelled = CTraderS011._to_session_dates(
            _utc_bars(["2026-09-07 21:00", "2026-09-08 21:00"], [100.0, 101.0]))
        assert [ts.date().isoformat() for ts in relabelled.index] == \
            ["2026-09-08", "2026-09-09"]

    def test_gmt_broker_midnight_open_keeps_its_own_date(self):
        relabelled = CTraderS011._to_session_dates(
            _utc_bars(["2026-09-07 00:00"], [100.0]))
        assert relabelled.index[0].date().isoformat() == "2026-09-07"

    def test_closes_are_untouched(self):
        """Relabelling must move the index only -- never the OHLC."""
        bars = _utc_bars(["2026-09-07 22:00"], [123.45])
        assert CTraderS011._to_session_dates(bars)["close"].tolist() == [123.45]

    def test_empty_frame_is_passed_through(self):
        assert CTraderS011._to_session_dates(pd.DataFrame()).empty

    def test_the_caller_is_not_mutated(self):
        """decide() and the alignment script must not see a frame whose index
        was rewritten underneath them."""
        bars = _utc_bars(["2026-09-07 21:00"], [100.0])
        CTraderS011._to_session_dates(bars)
        assert bars.index[0].date().isoformat() == "2026-09-07"


class TestRunLiveCycleMulti:
    def test_bars_reach_decide_session_dated(self):
        broker = FakeBroker(bars_by_symbol={
            "GER40": _utc_bars(["2026-09-06 21:00", "2026-09-07 21:00"], [100.0, 101.0])})
        seen = {}

        def decide(daily_bars, positions, balance, symbol_meta, last_price, resolved):
            seen.update(bars=daily_bars["DAX"], balance=balance, last_price=last_price)
            return []

        result = _strategy(broker).run_live_cycle_multi({"DAX": ("GER40",)}, 300, decide)

        assert [ts.date().isoformat() for ts in seen["bars"].index] == \
            ["2026-09-07", "2026-09-08"]
        assert seen["balance"] == 10_000.0
        assert result["resolved"] == {"DAX": "GER40"}

    def test_unresolved_assets_are_reported_not_dropped(self):
        broker = FakeBroker(bars_by_symbol={
            "GER40": _utc_bars(["2026-09-06 21:00"], [100.0])})
        result = _strategy(broker).run_live_cycle_multi(
            {"DAX": ("GER40",), "NASDAQ": ("USTEC",)}, 300, lambda *a: [])
        assert result["unresolved"] == ["NASDAQ"]

    def test_a_rejected_order_does_not_abort_the_other_instruments(self):
        """The live failure mode this protects: one MARKET_CLOSED instrument
        used to be able to cost the whole portfolio its cycle."""
        broker = FakeBroker(
            bars_by_symbol={
                "GER40": _utc_bars(["2026-09-06 21:00"], [100.0]),
                "US500": _utc_bars(["2026-09-06 21:00"], [200.0]),
            },
            reject_symbols={"GER40"})

        def decide(daily_bars, positions, balance, symbol_meta, last_price, resolved):
            return [
                dict(kind="open", asset="DAX", symbol="GER40", side="buy",
                     notional=1000.0, label="S011:DAX:2026-09-07"),
                dict(kind="open", asset="SP500", symbol="US500", side="buy",
                     notional=1000.0, label="S011:SP500:2026-09-07"),
            ]

        result = _strategy(broker).run_live_cycle_multi(
            {"DAX": ("GER40",), "SP500": ("US500",)}, 300, decide)

        failed = [r for r in result["results"] if r["error"] is not None]
        assert [r["action"]["asset"] for r in failed] == ["DAX"]
        assert [order["symbol"] for order in broker.orders] == ["US500"]

    def test_orders_are_priced_off_the_last_closed_bar(self):
        """Sizing off a still-forming bar oversized a live order ~5.7x on
        2026-08-19; the forming bar must never reach notional_price."""
        today_open = pd.Timestamp(datetime.now(timezone.utc).date())
        broker = FakeBroker(bars_by_symbol={
            "GER40": _utc_bars(
                [today_open - pd.Timedelta(days=2), today_open - pd.Timedelta(hours=3)],
                [100.0, 999.0])})

        def decide(daily_bars, positions, balance, symbol_meta, last_price, resolved):
            return [dict(kind="open", asset="DAX", symbol="GER40", side="buy",
                         notional=1000.0, label="S011:DAX:x")]

        _strategy(broker).run_live_cycle_multi({"DAX": ("GER40",)}, 300, decide)
        assert broker.orders[0]["notional_price"] == 100.0

    def test_close_actions_go_through_close_position(self):
        broker = FakeBroker(
            bars_by_symbol={"GER40": _utc_bars(["2026-09-06 21:00"], [100.0])},
            positions=[dict(position_id=77, label="S011:DAX:2026-09-01", volume=500)])

        def decide(daily_bars, positions, balance, symbol_meta, last_price, resolved):
            assert positions[0]["position_id"] == 77
            return [dict(kind="close", asset="DAX", position_id=77, volume=500,
                         label="S011:DAX:2026-09-01")]

        _strategy(broker).run_live_cycle_multi({"DAX": ("GER40",)}, 300, decide)
        assert broker.closed == [(77, 500)]


class TestFetchSessionDatedBars:
    def test_returns_relabelled_bars_and_the_resolution(self):
        broker = FakeBroker(bars_by_symbol={
            "GER40": _utc_bars(["2026-09-07 21:00"], [100.0])})
        bars_by_asset, resolved, unresolved = _strategy(broker).fetch_session_dated_bars(
            {"DAX": ("GER40",), "NASDAQ": ("USTEC",)}, 300)
        assert resolved == {"DAX": "GER40"}
        assert unresolved == ["NASDAQ"]
        assert bars_by_asset["DAX"].index[0].date().isoformat() == "2026-09-08"

    def test_it_sends_no_orders(self):
        """The alignment script relies on this being strictly read-only."""
        broker = FakeBroker(bars_by_symbol={
            "GER40": _utc_bars(["2026-09-07 21:00"], [100.0])})
        _strategy(broker).fetch_session_dated_bars({"DAX": ("GER40",)}, 300)
        assert broker.orders == [] and broker.closed == []


@pytest.mark.parametrize("period_arg", ["D1"])
def test_the_cycle_always_asks_for_d1(period_arg):
    """FakeBroker.get_trendbars_many asserts the period, so a change of bar
    period in the cycle would fail here rather than silently trade off M1."""
    broker = FakeBroker(bars_by_symbol={"GER40": _utc_bars(["2026-09-07 21:00"], [100.0])})
    _strategy(broker).run_live_cycle_multi({"DAX": ("GER40",)}, 300, lambda *a: [])
