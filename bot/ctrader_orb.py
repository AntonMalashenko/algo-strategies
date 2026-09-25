"""cTrader adapter extension for S021 (ORB) -- adds STOP-order entries and a
resting-order-aware live cycle on top of CTraderS007 (reuses its M1/close/
deal-list/session plumbing; only the entry mechanics differ).

Why STOP orders, not MARKET (architecture decision, chat with Anton
2026-09-21, see strategy-passport-S021.md sec 8): S021's entry levels (U, L)
are both fully known at session open (O and the causal ADR14 are both
available by 09:30) -- unlike S007's reactive per-minute signal detection,
there is nothing to "wait and see" before the levels can be set. A resting
STOP order sitting at the broker fills at the broker's own matching engine
the instant price touches it, independent of this bot's own poll-cycle
latency (the class of problem documented for S007 in
claude/research-2026-09-15-s007-execution-latency.md) -- for S021 that
latency class only ever affects the ENTRY, and placing both boundary orders
at session open removes it entirely for entries. It does NOT remove it for
the 15:59 time-based exit, which still depends on this bot's cycle actually
running around that time -- see run_live_cycle_orb's docstring.

Two orders are placed at session open: a BUY STOP at U and a SELL STOP at L,
each with its own SL (0.75*ADR14 from that order's own price, per sec 3) and
deliberately no takeProfit (sec 3/10: "не добавлять тейк-профит"). Whichever
fills first becomes the day's position; the other is cancelled as soon as a
cycle sees a position appear for today's label (bot/orb_signals.py::decide).
A bar-close double-touch has no clean resting-order equivalent -- a real
price gap could in principle fill both orders before either cancel reaches
the broker. decide() detects that (both today's labels showing an open
position simultaneously) and flattens both immediately as an execution
anomaly, logged loudly -- not silently absorbed. This has NEVER been
observed live (S021 has no live history yet); flag it if it ever fires.

Two trendbar fetches per cycle, not one (fixed 2026-09-22): ADR14's HISTORY
comes from M15 bars (_get_m15_step, `history_days` window) and only "today"
comes from M1 bars (_get_m1_step, a small `today_days` window -- the 09:30
anchor bar plus a precise "now" reading for decide()'s 14:29/15:59 checks).
Root cause of the split: cTrader's ProtoOAGetTrendbarsReq caps a response at
roughly 14000 bars regardless of the requested window (observed server
behavior, not documented by the API). On M1 that is only ~13 session days,
below OrbConfig.adr_window=14, so ADR14 was always NaN and the bot never
placed an order. See _get_m15_step for why M15 is an exact substitute for M1
in the ADR14 computation, not an approximation.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd

from bot.ctrader import HAVE_SDK, Protobuf
from bot.ctrader_s007 import CTraderS007, PRICE_SCALE

if HAVE_SDK:
    from twisted.internet import defer
    from ctrader_open_api.messages.OpenApiMessages_pb2 import (
        ProtoOAGetTrendbarsReq, ProtoOANewOrderReq, ProtoOACancelOrderReq,
        ProtoOAReconcileReq,
    )
    from ctrader_open_api.messages.OpenApiModelMessages_pb2 import (
        ProtoOAOrderType, ProtoOATradeSide, ProtoOATrendbarPeriod,
        ProtoOAOrderStatus, ProtoOATimeInForce,
    )

# histdata's/S021's frozen session anchor is a FIXED UTC-5 (EST, no DST)
# clock reading -- see strategies/orb_intraday/engine.py's module docstring
# for the full investigation. cTrader trendbars arrive as UTC minute stamps;
# converting them to this fixed offset (never DST-adjusted, unlike
# CTraderS007.get_m1's "Europe/Bucharest" conversion for GER40/S007) is what
# lets bot/orb_config.STRATEGY.session_open == time(9, 30) mean exactly what
# the backtest means by it. Matches engine.py's HISTDATA_FIXED_OFFSET.
FIXED_EST_OFFSET = "Etc/GMT+5"


class CTraderORB(CTraderS007):
    """Adds STOP-order placement/cancellation and pending-order-aware
    reconcile on top of CTraderS007. Deliberately subclasses CTraderS007
    (not CTraderAdapter directly) to reuse its M1/close/deal-list/session
    plumbing -- see strategy-passport-S021.md sec 8's "open architectural
    decision": get_m1/place_market/close_position arguably belong in the
    shared bot/ctrader.py base, not S007's own module, but moving them
    touches working S007 code and needs Anton's explicit sign-off first;
    this subclass is the stated interim workaround, same as S007 itself
    subclassing the S004-era CTraderAdapter.
    """

    # ---------- M1/M15 on the fixed-EST clock (overrides CTraderS007's
    # Europe/Bucharest conversion -- see module docstring) ----------

    def _trendbars_req(self, symbol: str, days: int, period=None):
        """ProtoOAGetTrendbarsReq for the last `days` calendar days of
        `symbol` at `period` (a ProtoOATrendbarPeriod value; None means M1,
        kept as the default so the existing M1 callers are unchanged).

        `period` is added so the ADR14 history can be fetched as M15 (see
        _get_m15_step): cTrader caps a trendbars response at roughly 14000
        bars whatever `days` asks for, which on M1 covers only ~13 session
        days -- below OrbConfig.adr_window=14.

        The M1 default is resolved in the body, NOT as a default-parameter
        value: ProtoOATrendbarPeriod is only imported when bot.ctrader.HAVE_SDK
        is True, and a signature default is evaluated at import time, so it
        would add a NameError at import on a machine without the
        ctrader-open-api package. (This module's `from bot.ctrader import
        Protobuf` already needs the SDK at import time today -- a
        pre-existing limitation, not introduced here -- but this method
        should not add a second one.)"""
        req = ProtoOAGetTrendbarsReq()
        req.ctidTraderAccountId = self.account
        req.symbolId = self.symbol_id(symbol)
        req.period = period if period is not None else ProtoOATrendbarPeriod.M1
        now = datetime.now(timezone.utc)
        req.fromTimestamp = int((now - timedelta(days=days)).timestamp() * 1000)
        req.toTimestamp = int(now.timestamp() * 1000)
        return req

    @staticmethod
    def _parse_trendbars_fixed_est(resp) -> pd.DataFrame:
        msg = Protobuf.extract(resp)
        rows = []
        for tb in msg.trendbar:
            lo = tb.low
            rows.append(dict(ts=tb.utcTimestampInMinutes * 60,
                             open=(lo + tb.deltaOpen) / PRICE_SCALE,
                             high=(lo + tb.deltaHigh) / PRICE_SCALE,
                             low=lo / PRICE_SCALE,
                             close=(lo + tb.deltaClose) / PRICE_SCALE))
        df = pd.DataFrame(rows)
        idx = (pd.to_datetime(df.pop("ts"), unit="s", utc=True)
               .dt.tz_convert(FIXED_EST_OFFSET).dt.tz_localize(None))
        df.index = idx
        return df.sort_index()

    def get_m1(self, symbol: str, days: int) -> pd.DataFrame:
        """One-off M1 fetch (--check/--dry-run style use), fixed-EST clock."""
        def work(done):
            d = self._load_symbols()

            def ask(_m):
                return self.client.send(self._trendbars_req(symbol, days))
            d.addCallback(ask)
            d.addCallbacks(lambda resp: done(self._parse_trendbars_fixed_est(resp)),
                           lambda f: done(error=f))
        return self._run(work)

    def _get_m1_step(self, symbol: str, days: int):
        """Session-chainable version for run_live_cycle_orb (see
        CTraderS007's own comment on why these _step helpers exist: several
        ops must share ONE connect/auth session)."""
        d = self.client.send(self._trendbars_req(symbol, days))
        d.addCallback(self._parse_trendbars_fixed_est)
        return d

    def get_m15(self, symbol: str, days: int) -> pd.DataFrame:
        """One-off M15 fetch (--check/--dry-run/diagnostic-script style use,
        same as get_m1 but M15) -- same fixed-EST clock, see _get_m15_step.

        Overrides bot.ctrader.CTraderAdapter.get_m15, which returns raw
        points on a Europe/Bucharest clock for the S004-era bot; this
        version returns prices (divided by PRICE_SCALE) on the fixed-EST
        clock, same as this class's get_m1 override does for M1."""
        def work(done):
            d = self._load_symbols()

            def ask(_m):
                return self.client.send(
                    self._trendbars_req(symbol, days, period=ProtoOATrendbarPeriod.M15))
            d.addCallback(ask)
            d.addCallbacks(lambda resp: done(self._parse_trendbars_fixed_est(resp)),
                           lambda f: done(error=f))
        return self._run(work)

    def _get_m15_step(self, symbol: str, days: int):
        """Session-chainable M15 fetch (same pattern as _get_m1_step) used
        for the ADR14 HISTORY window in run_live_cycle_orb.

        Why M15 and not M1: cTrader's ProtoOAGetTrendbarsReq caps a response
        at roughly 14000 bars regardless of the requested window (observed
        server behavior, not documented by the API). On M1 that covers only
        ~13 session days -- below OrbConfig.adr_window=14 -- so ADR14 was
        always NaN live. M15 covers 15x more time per bar, so a 30-day
        window is a few thousand bars, far under the cap.

        Why M15 is an EXACT substitute for each day's session range, not an
        approximation: the session is
        09:30-15:59 fixed-EST inclusive, i.e. 390 minutes = 26 whole M15
        bars, and 09:30 is itself a 15-minute boundary (570 min from
        midnight, 570/15 = 38), as is the 16:00 end. Every M15 bar in that
        window therefore aggregates exactly M1 bars that are themselves
        in-session, with no partial-bar spillover across either boundary,
        and max-of-highs / min-of-lows is invariant to that sub-bar
        aggregation -- so session_high/session_low/session_range (all ADR14
        reads) come out identical to the M1-derived values. D1 does not
        work (the broker's daily-bar boundary is not the fixed-EST session)
        and neither does H1 (570/60 = 9.5, so 09:30 falls mid-bar and an H1
        bar would straddle the session open).

        The one thing that does NOT carry over unchanged is the day-validity
        rule (OrbConfig.min_session_bars is calibrated in M1 bars, and the
        backtest also requires an exact 09:30 M1 bar, neither of which M15
        bars can show) -- see bot/orb_signals.py::_m15_min_session_bars, the
        single place that granularity change needed its own threshold, for
        how it is scaled and the measured residual difference."""
        d = self.client.send(
            self._trendbars_req(symbol, days, period=ProtoOATrendbarPeriod.M15))
        d.addCallback(self._parse_trendbars_fixed_est)
        return d

    # ---------- pending-order-aware reconcile ----------

    def _reconcile_full_step(self):
        """Like CTraderS007._reconcile_step, but ALSO returns pending
        orders -- S021 needs to know about its own resting STOP orders,
        which base _reconcile_step (written for S007's market-order-only
        flow) never looks at (msg.order is ignored there). Only
        ORDER_STATUS_ACCEPTED orders are returned -- a filled/rejected/
        expired/cancelled order is no longer "pending" and decide() only
        ever needs to know what's still resting."""
        req = ProtoOAReconcileReq()
        req.ctidTraderAccountId = self.account
        d = self.client.send(req)

        def fin(resp):
            msg = Protobuf.extract(resp)
            positions = []
            for p in msg.position:
                td = p.tradeData
                positions.append(dict(
                    position_id=p.positionId, label=getattr(td, "label", "") or "",
                    side="buy" if td.tradeSide == ProtoOATradeSide.BUY else "sell",
                    volume=td.volume, price=p.price, stop_loss=p.stopLoss,
                    symbol_id=td.symbolId, opened_ts=td.openTimestamp))
            orders = []
            for o in msg.order:
                if o.orderStatus != ProtoOAOrderStatus.ORDER_STATUS_ACCEPTED:
                    continue
                td = o.tradeData
                orders.append(dict(
                    order_id=o.orderId, label=getattr(td, "label", "") or "",
                    side="buy" if td.tradeSide == ProtoOATradeSide.BUY else "sell",
                    stop_price=o.stopPrice, stop_loss=o.stopLoss,
                    symbol_id=td.symbolId))
            return dict(positions=positions, orders=orders)
        d.addCallback(fin)
        return d

    # ---------- order placement/cancellation ----------

    def _place_stop_step(self, symbol: str, side: str, stop_price: float,
                         sl_price: float, volume_lots: float, label: str,
                         full_symbol=None):
        """STOP order (buy-stop above market / sell-stop below), SL
        attached, deliberately NO takeProfit field set -- S021 sec 3/10:
        'не добавлять тейк-профит', exit is time-based (15:59) or stop
        only. GOOD_TILL_CANCEL, not an expiring GTD order: the bot itself
        cancels an unfilled order at the entry cutoff (14:29, see
        bot/orb_signals.py::decide) so that decision is explicit and logged
        rather than left to broker-side expiry semantics."""
        @defer.inlineCallbacks
        def flow():
            full = full_symbol
            if full is None:
                full = yield self._get_full_symbol_step(symbol)
            sym = self._symbols[symbol.upper()]
            req = ProtoOANewOrderReq()
            req.ctidTraderAccountId = self.account
            req.symbolId = sym.symbolId
            req.orderType = ProtoOAOrderType.STOP
            req.tradeSide = ProtoOATradeSide.BUY if side == "buy" else ProtoOATradeSide.SELL
            req.volume = self._volume_from_lots(volume_lots, full)
            req.timeInForce = ProtoOATimeInForce.GOOD_TILL_CANCEL
            # Rounded to the symbol's own price precision -- same reasoning
            # as CTraderS007._place_market_step (cTrader's INVALID_REQUEST
            # rejects extra decimal digits; found live for S007 2026-08-06).
            req.stopPrice = round(float(stop_price), full.digits)
            req.stopLoss = round(float(sl_price), full.digits)
            req.label = label
            req.comment = "S021"
            d = self.client.send(req)
            d.addCallback(self._check_response)
            result = yield d
            return result
        return flow()

    def _cancel_order_step(self, order_id: int):
        req = ProtoOACancelOrderReq()
        req.ctidTraderAccountId = self.account
        req.orderId = order_id
        d = self.client.send(req)
        d.addCallback(self._check_response)
        return d

    # ---------- single-session live cycle ----------

    def run_live_cycle_orb(self, symbol_candidates, history_days: int, today_days: int,
                           decide):
        """One connect/auth/work/disconnect session for a full ORB cycle --
        parallels CTraderS007.run_live_cycle, but reads pending orders too
        (_reconcile_full_step) and dispatches ORB's action vocabulary
        (place_stop / cancel_order / close) instead of S007's
        (place / amend / close). See bot/orb_signals.py::decide for what
        each action kind means and when it's emitted; a detected resting-
        order fill is handled inside decide() itself (pure bookkeeping, no
        broker call) and never appears in the action list this method
        dispatches.

        NOTE on latency (see module docstring): only the ENTRY (place_stop,
        placed once at session open, filled at the broker independent of
        this cycle) is immune to this bot's own cycle latency. The 15:59
        time exit is an ACTIVE action this method must still send in a
        timely cycle -- if the scheduler's tick is delayed or this process
        is slow to start, the position stays open past 15:59 until the next
        successful cycle reaches it. Not solved here; see
        strategy-passport-S021.md sec 8 for the open item.

        Two trendbar windows are fetched in parallel (see module docstring):
          - `history_days`: the M15 window ADR14's HISTORY is reconstructed
            from (_get_m15_step). NOTE: until 2026-09-22 this was the M1
            window and had to cover both history and today -- it now means
            M15 history only.
          - `today_days`: a small M1 window (_get_m1_step) that only needs to
            cover "today" -- the 09:30 anchor bar and a precise "now"
            reading for decide()'s 14:29/15:59 checks. It does NOT need to
            cover ADR history anymore.
        decide() is called as decide(symbol, m1, m15, positions, orders,
        balance, money_per_point_per_lot).

        Returns {"symbol", "m1", "m15", "positions", "orders", "actions", "results",
        "balance", "money_per_point_per_lot"} -- results[i] = {"action",
        "result", "error"} lines up with actions, same contract as
        CTraderS007.run_live_cycle.
        """
        def work(done):
            @defer.inlineCallbacks
            def flow():
                yield self._load_symbols()
                up = {n.upper() for n in self._symbols.keys()}
                symbol = next((c for c in symbol_candidates if c.upper() in up), None)
                if symbol is None:
                    raise RuntimeError(
                        f"none of {symbol_candidates} found; broker symbols "
                        f"e.g. {sorted(self._symbols.keys())[:15]}")

                full_symbol, balance, m1, m15, reconciled = yield defer.gatherResults(
                    [self._get_full_symbol_step(symbol), self._get_balance_step(),
                     self._get_m1_step(symbol, today_days),
                     self._get_m15_step(symbol, history_days),
                     self._reconcile_full_step()],
                    consumeErrors=True)
                money_per_point_per_lot = full_symbol.lotSize

                actions = decide(symbol, m1, m15, reconciled["positions"], reconciled["orders"],
                                 balance, money_per_point_per_lot)

                results = []
                for a in actions:
                    try:
                        if a["kind"] == "place_stop":
                            r = yield self._place_stop_step(
                                symbol, a["side"], a["stop"], a["sl"], a["volume_lots"],
                                a["label"], full_symbol=full_symbol)
                        elif a["kind"] == "cancel_order":
                            r = yield self._cancel_order_step(a["order_id"])
                        elif a["kind"] == "close":
                            r = yield self._close_position_step(a["position_id"], a["volume"])
                        else:
                            raise RuntimeError(f"unknown ORB action kind: {a['kind']!r}")
                        results.append(dict(action=a, result=r, error=None))
                    except Exception as e:
                        results.append(dict(action=a, result=None, error=e))

                return dict(symbol=symbol, m1=m1, m15=m15, positions=reconciled["positions"],
                            orders=reconciled["orders"], actions=actions, results=results,
                            balance=balance, money_per_point_per_lot=money_per_point_per_lot)

            d = flow()
            d.addCallbacks(lambda r: done(r), lambda f: done(error=f))
        return self._run(work)
