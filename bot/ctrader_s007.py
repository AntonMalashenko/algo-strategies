"""cTrader adapter extension for S007 — adds M1 bars, MARKET orders and position
close on top of the S004 `CTraderAdapter` (same connection/auth/credentials).

Reuses everything from bot.ctrader; only the pieces S007 needs that S004 didn't
(index instrument, market entries, pyramiding, position close) are added here.

NOTE (as in bot/ctrader.py): field/enum names follow the Open API spec; a couple
of order/price details can differ by SDK version and broker — run `--check` and
`--dry-run` first, then verify the first few live orders against the cTrader UI.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import pandas as pd

from bot.ctrader import CTraderAdapter, HAVE_SDK, Protobuf  # reuse S004 adapter

if HAVE_SDK:
    from twisted.internet import defer
    from ctrader_open_api import Client, TcpProtocol, EndPoints
    from ctrader_open_api.messages.OpenApiMessages_pb2 import (
        ProtoOAApplicationAuthReq, ProtoOAGetTrendbarsReq, ProtoOANewOrderReq,
        ProtoOAClosePositionReq, ProtoOAReconcileReq, ProtoOASymbolByIdReq,
        ProtoOADealListReq, ProtoOAOrderErrorEvent, ProtoOAErrorRes,
        ProtoOAAmendPositionSLTPReq,
    )
    from ctrader_open_api.messages.OpenApiModelMessages_pb2 import (
        ProtoOAOrderType, ProtoOATradeSide, ProtoOATrendbarPeriod,
        ProtoOADealStatus,
    )

PRICE_SCALE = 100000.0   # Open API trendbar/price integers = human_price * 1e5


class CTraderS007(CTraderAdapter):
    def __init__(self, creds: dict | None = None, require_account: bool = True):
        """creds=None -> read from .env (single-account mode, same as S004).
        creds dict -> explicit per-account credentials (multi-account runner):
        {client_id, client_secret, access_token, account_id, host?}."""
        if not HAVE_SDK:
            raise RuntimeError("pip install ctrader-open-api first")
        if creds is None:
            super().__init__(require_account=require_account)
            return
        self.client_id = creds["client_id"]
        self.secret = creds["client_secret"]
        self.token = creds["access_token"]
        self.account = int(creds.get("account_id") or 0)
        host = creds.get("host") or EndPoints.PROTOBUF_DEMO_HOST
        need = [self.client_id, self.secret, self.token]
        if require_account:
            need.append(self.account)
        if not all(need):
            raise RuntimeError("missing per-account cTrader credentials")
        self.client = Client(host, EndPoints.PROTOBUF_PORT, TcpProtocol)
        self._result = None
        self._error = None
        self._symbols = None
        self._session_timings: dict = {}  # see CTraderAdapter.__init__/_run

    def resolve_symbol(self, candidates) -> str:
        """First matching broker symbol name from a candidate list (GER40/DE40/...)."""
        def work(done):
            d = self._load_symbols()
            d.addCallbacks(lambda _m: done(dict(names=list(self._symbols.keys()))),
                           lambda f: done(error=f))
        names = self._run(work)["names"]
        up = {n.upper() for n in names}
        for c in candidates:
            if c.upper() in up:
                return c
        raise RuntimeError(f"none of {candidates} found; broker symbols e.g. {sorted(names)[:15]}")

    def get_m1(self, symbol: str, days: int) -> pd.DataFrame:
        """M1 bars in HUMAN index points, naive EET index (engine convention)."""
        def work(done):
            d = self._load_symbols()

            def ask(_m):
                req = ProtoOAGetTrendbarsReq()
                req.ctidTraderAccountId = self.account
                req.symbolId = self.symbol_id(symbol)
                req.period = ProtoOATrendbarPeriod.M1
                now = datetime.now(timezone.utc)
                req.fromTimestamp = int((now - timedelta(days=days)).timestamp() * 1000)
                req.toTimestamp = int(now.timestamp() * 1000)
                return self.client.send(req)

            def fin(resp):
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
                       .dt.tz_convert("Europe/Bucharest").dt.tz_localize(None))
                df.index = idx
                done(df.sort_index())
            d.addCallback(ask)
            d.addCallbacks(fin, lambda f: done(error=f))
        return self._run(work)

    def open_positions(self) -> list:
        """Open positions as dicts: {position_id, label, side, volume}."""
        st = self.reconcile()
        out = []
        for p in st["positions"]:
            td = p.tradeData
            out.append(dict(
                position_id=p.positionId,
                label=getattr(td, "label", "") or "",
                side="buy" if td.tradeSide == ProtoOATradeSide.BUY else "sell",
                volume=td.volume,
            ))
        return out

    def place_market(self, symbol: str, side: str, sl_price: float, tp_price: float,
                     volume_lots: float, label: str, contract_size: float = 1.0):
        """Market order with absolute SL/TP (human prices). Volume in lots,
        converted via the instrument's real lotSize/min/max/stepVolume
        (see _volume_from_lots) -- verify vs the cTrader UI on first run."""
        def work(done):
            d = self._load_symbols()
            d.addCallback(lambda _m: self._place_market_step(
                symbol, side, sl_price, tp_price, volume_lots, label))
            d.addCallbacks(lambda r: done(r), lambda f: done(error=f))
        return self._run(work)

    def close_position(self, position_id: int, volume: int):
        def work(done):
            req = ProtoOAClosePositionReq()
            req.ctidTraderAccountId = self.account
            req.positionId = position_id
            req.volume = volume
            d = self.client.send(req)
            d.addCallback(self._check_response)
            d.addCallbacks(lambda r: done(r), lambda f: done(error=f))
        return self._run(work)

    def amend_position_sl(self, position_id: int, sl_price: float,
                          tp_price: float | None):
        """Move an open position's stop loss (ALGODEV-37 live breakeven).
        Standalone one-off variant of _amend_position_sltp_step below --
        see that method for the takeProfit-preservation contract."""
        def work(done):
            d = self._amend_position_sltp_step(position_id, sl_price, tp_price)
            d.addCallbacks(lambda r: done(r), lambda f: done(error=f))
        return self._run(work)

    # ---------- single-session live cycle ----------
    #
    # The methods above each open their own connect/auth/work/disconnect
    # session (via _run). That's fine for one-off calls (--check, --accounts),
    # but the demo server drops a fresh connection reconnected too soon after
    # the previous one closed -- so a --live cycle, which needs several
    # operations back to back (resolve symbol, get M1 bars, list positions,
    # place/close orders), must do them all inside ONE session. These _step
    # helpers assume the session is already connected+authed (no _run/reconnect
    # of their own) and are chained together by run_live_cycle below.

    def _get_m1_step(self, symbol: str, days: int):
        req = ProtoOAGetTrendbarsReq()
        req.ctidTraderAccountId = self.account
        req.symbolId = self.symbol_id(symbol)
        req.period = ProtoOATrendbarPeriod.M1
        now = datetime.now(timezone.utc)
        req.fromTimestamp = int((now - timedelta(days=days)).timestamp() * 1000)
        req.toTimestamp = int(now.timestamp() * 1000)
        d = self.client.send(req)

        def fin(resp):
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
                   .dt.tz_convert("Europe/Bucharest").dt.tz_localize(None))
            df.index = idx
            return df.sort_index()
        d.addCallback(fin)
        return d

    def _reconcile_step(self):
        req = ProtoOAReconcileReq()
        req.ctidTraderAccountId = self.account
        d = self.client.send(req)

        def fin(resp):
            msg = Protobuf.extract(resp)
            out = []
            for p in msg.position:
                td = p.tradeData
                out.append(dict(
                    position_id=p.positionId,
                    label=getattr(td, "label", "") or "",
                    side="buy" if td.tradeSide == ProtoOATradeSide.BUY else "sell",
                    volume=td.volume,
                    # broker's own fill price / current SL, not our requested
                    # values -- needed to sum real potential loss across open
                    # positions (bot/s007_paper.py's daily risk cap).
                    price=p.price,
                    stop_loss=p.stopLoss,
                    # additive, for sync_snapshot: which instrument this is, so
                    # broker volume units can be turned back into lots and an
                    # unknown ("adopted") position can be named. Existing
                    # callers ignore the extra keys.
                    symbol_id=td.symbolId,
                    take_profit=p.takeProfit,
                    opened_ts=td.openTimestamp,
                ))
            return out
        d.addCallback(fin)
        return d

    # ---------- read-only account snapshot (webapp/sync_positions.py) --------

    # cTrader rejects a ProtoOADealListReq spanning more than a week, so a
    # longer lookback has to be split into <=7-day windows and stitched here.
    _DEAL_WINDOW_DAYS = 7

    def _deal_list_step(self, from_ms: int, to_ms: int, max_rows: int = 1000):
        """Closing deals in [from_ms, to_ms). Read-only: places nothing.

        Only deals that actually closed something are returned -- a deal
        carries `closePositionDetail` exactly when it reduced/closed a
        position, and that sub-message (not the deal itself) is where the
        realised money lives. Opening deals are dropped: the DB already has
        the entry from when the runner opened the row.
        """
        req = ProtoOADealListReq()
        req.ctidTraderAccountId = self.account
        req.fromTimestamp = int(from_ms)
        req.toTimestamp = int(to_ms)
        req.maxRows = max_rows
        d = self.client.send(req)
        d.addCallback(self._check_response)
        d.addCallback(self._parse_deals)
        return d

    @staticmethod
    def _parse_deals(msg) -> list:
        ok = (ProtoOADealStatus.FILLED, ProtoOADealStatus.PARTIALLY_FILLED)
        out = []
        for dl in msg.deal:
            if dl.dealStatus not in ok or not dl.HasField("closePositionDetail"):
                continue
            cpd = dl.closePositionDetail
            # Money fields are int64 scaled by 10^moneyDigits (moneyDigits is
            # per-message, NOT a constant -- a JPY-deposit account reports a
            # different scale than a USD one), so never hardcode /100 here.
            scale = 10.0 ** (cpd.moneyDigits or dl.moneyDigits or 2)
            gross = cpd.grossProfit / scale
            swap = cpd.swap / scale
            comm = cpd.commission / scale
            out.append(dict(
                deal_id=dl.dealId,
                position_id=dl.positionId,
                symbol_id=dl.symbolId,
                side="buy" if dl.tradeSide == ProtoOATradeSide.BUY else "sell",
                # plain doubles in the Open API, unlike trendbars -- do NOT
                # divide these by PRICE_SCALE.
                exit_price=dl.executionPrice,
                entry_price=cpd.entryPrice,
                closed_volume=cpd.closedVolume,
                gross_profit=gross,
                swap=swap,
                commission=comm,
                # swap and commission arrive already signed, so the net is a
                # plain sum, not gross - fees.
                pnl=gross + swap + comm,
                balance_after=cpd.balance / scale,
                executed_ms=dl.executionTimestamp,
            ))
        return out

    def _lot_sizes_step(self, symbol_ids):
        """{symbol_id: lotSize} in one request (symbolId is repeated), so a
        multi-instrument account costs one round trip, not one per symbol."""
        if not symbol_ids:
            return defer.succeed({})
        req = ProtoOASymbolByIdReq()
        req.ctidTraderAccountId = self.account
        for sid in symbol_ids:
            req.symbolId.append(int(sid))
        d = self.client.send(req)
        d.addCallback(self._check_response)
        d.addCallback(lambda m: {s.symbolId: s.lotSize for s in m.symbol})
        return d

    @staticmethod
    def _lots_from_volume(volume: int, lot_size) -> float | None:
        """Inverse of _volume_from_lots. None (not 0.0) when the instrument's
        lotSize is unknown -- a wrong lot figure in the UI is worse than a
        blank one."""
        if not lot_size:
            return None
        return volume / (100.0 * lot_size)

    def sync_snapshot(self, days: int = 7) -> dict:
        """Everything webapp/sync_positions.py needs, in ONE session.

        Read-only by construction: reconcile + deal list + symbol metadata,
        no order is ever sent from here. It has to be a single session for
        the same reason run_live_cycle does -- the demo server drops a fresh
        connection reconnected too soon after the previous one closed.

        Returns {"positions", "deals", "symbols_by_id", "lot_sizes",
        "fetched_at"}; positions/deals carry `symbol` and `volume_lots`.
        """
        def work(done):
            @defer.inlineCallbacks
            def flow():
                yield self._load_symbols()
                by_id = {ls.symbolId: name for name, ls in self._symbols.items()}

                positions = yield self._reconcile_step()

                now = datetime.now(timezone.utc)
                deals = []
                start = now - timedelta(days=max(1, int(days)))
                while start < now:
                    end = min(start + timedelta(days=self._DEAL_WINDOW_DAYS), now)
                    chunk = yield self._deal_list_step(
                        int(start.timestamp() * 1000), int(end.timestamp() * 1000))
                    deals.extend(chunk)
                    start = end

                need = ({p["symbol_id"] for p in positions}
                        | {d["symbol_id"] for d in deals})
                lot_sizes = yield self._lot_sizes_step(sorted(need))

                for p in positions:
                    p["symbol"] = by_id.get(p["symbol_id"], "")
                    p["volume_lots"] = self._lots_from_volume(
                        p["volume"], lot_sizes.get(p["symbol_id"]))
                for dl in deals:
                    dl["symbol"] = by_id.get(dl["symbol_id"], "")
                    dl["volume_lots"] = self._lots_from_volume(
                        dl["closed_volume"], lot_sizes.get(dl["symbol_id"]))

                return dict(positions=positions, deals=deals,
                            symbols_by_id=by_id, lot_sizes=lot_sizes,
                            fetched_at=now)

            d = flow()
            d.addCallbacks(lambda r: done(r), lambda f: done(error=f))
        return self._run(work)

    @staticmethod
    def _check_response(resp):
        """client.send() only errbacks on transport failures -- an
        application-level rejection (wrong volume, bad price, market closed,
        ...) comes back as a normal response. Two known-bad shapes:

        (1) Protobuf.extract() resolves it to a named error message
            (ProtoOAErrorRes / ProtoOAOrderErrorEvent) -- straightforward,
            raise from its errorCode/description.
        (2) Protobuf.extract() does NOT resolve it and hands back the raw
            envelope (payloadType + payload bytes) instead of the specific
            message class. CONFIRMED live 2026-07-20: a TRADING_BAD_VOLUME
            rejection came back exactly this way, and the old version of
            this method (which only checked case 1) treated it as success --
            the bot logged "ok=True" for 6 orders that the broker had fully
            rejected (0 volume ever placed). See decisions-log.md 2026-07-21.

        For (2): the raw envelope has payloadType/payload fields but not the
        errorCode field a decoded message would have -- that's the tell.
        Try to decode the payload bytes as either known error message; if
        that also comes up empty, refuse to guess "success" and raise loud
        instead, with the raw payloadType so the failure is diagnosable.
        """
        msg = Protobuf.extract(resp)
        name = type(msg).__name__
        if name in ("ProtoOAErrorRes", "ProtoOAOrderErrorEvent"):
            raise RuntimeError(f"{msg.errorCode}: {getattr(msg, 'description', '')}")
        if hasattr(msg, "payloadType") and hasattr(msg, "payload") and not hasattr(msg, "errorCode"):
            for cls in (ProtoOAOrderErrorEvent, ProtoOAErrorRes):
                candidate = cls()
                try:
                    candidate.ParseFromString(msg.payload)
                except Exception:
                    continue
                if candidate.errorCode:
                    raise RuntimeError(f"{candidate.errorCode}: {getattr(candidate, 'description', '')}")
            raise RuntimeError(
                f"unrecognized broker response (payloadType={msg.payloadType}) -- "
                f"treating as a failure, not assuming success")
        return msg

    def _get_full_symbol_step(self, symbol: str):
        """ProtoOASymbolsListReq only returns ProtoOALightSymbol (name/id/category
        -- no lotSize/minVolume/maxVolume/stepVolume). Those trading params need
        a separate ProtoOASymbolByIdReq; required to size orders correctly."""
        sym_id = self._symbols[symbol.upper()].symbolId
        req = ProtoOASymbolByIdReq()
        req.ctidTraderAccountId = self.account
        req.symbolId.append(sym_id)
        d = self.client.send(req)
        d.addCallback(self._check_response)
        d.addCallback(lambda msg: msg.symbol[0])
        return d

    @staticmethod
    def _volume_from_lots(volume_lots: float, full_symbol) -> int:
        """lots -> Open API volume units, clamped to [minVolume, maxVolume]
        and rounded to a stepVolume multiple (a raw lots*100*lotSize can
        otherwise exceed the broker's max, e.g. an FX-sized 0.01 lot is way
        too big for an index CFD with lotSize in the hundreds, not 100000)."""
        step = full_symbol.stepVolume or 1
        raw = volume_lots * 100 * full_symbol.lotSize
        raw = max(full_symbol.minVolume, min(full_symbol.maxVolume, raw))
        return int(round(raw / step) * step)

    def _place_market_step(self, symbol: str, side: str, sl_price: float, tp_price: float,
                           volume_lots: float, label: str, full_symbol=None):
        """full_symbol: pass the already-fetched ProtoOASymbol (e.g. from
        run_live_cycle, which fetches it once per cycle for risk sizing) to
        skip a redundant ProtoOASymbolByIdReq per order; omit to fetch it
        fresh (standalone/one-off use)."""
        @defer.inlineCallbacks
        def flow():
            full = full_symbol
            if full is None:
                full = yield self._get_full_symbol_step(symbol)
            sym = self._symbols[symbol.upper()]
            req = ProtoOANewOrderReq()
            req.ctidTraderAccountId = self.account
            req.symbolId = sym.symbolId
            req.orderType = ProtoOAOrderType.MARKET
            req.tradeSide = ProtoOATradeSide.BUY if side == "buy" else ProtoOATradeSide.SELL
            req.volume = self._volume_from_lots(volume_lots, full)
            # Round to the symbol's own price precision (full.digits, e.g. 2
            # for DE40) before sending: sl_price/tp_price are derived through
            # several float ops (range mid, swing stops, etc.) on quotes that
            # arrive with up to 5 decimals (see PRICE_SCALE), so an unrounded
            # value routinely comes out as e.g. 26081.200000000004 -- valid
            # Python float, but cTrader's INVALID_REQUEST rejects anything
            # with more decimal digits than the symbol allows (found live
            # 2026-08-06: every cycle that minute rejected with "Order price
            # ... has more digits than symbol allows", see decisions-log.md).
            req.stopLoss = round(float(sl_price), full.digits)
            req.takeProfit = round(float(tp_price), full.digits)
            req.label = label
            req.comment = "S007"
            d = self.client.send(req)
            d.addCallback(self._check_response)
            result = yield d
            return result
        return flow()

    def _close_position_step(self, position_id: int, volume: int):
        req = ProtoOAClosePositionReq()
        req.ctidTraderAccountId = self.account
        req.positionId = position_id
        req.volume = volume
        d = self.client.send(req)
        d.addCallback(self._check_response)
        return d

    def _amend_position_sltp_step(self, position_id: int, sl_price: float,
                                  tp_price: float | None, digits: int = 2):
        """Amend an open position's SL (ALGODEV-37 live breakeven at 0.5R).

        ProtoOAAmendPositionSLTPReq REPLACES both protection levels: a field
        left unset is REMOVED from the position, not "kept as is" -- so the
        position's current takeProfit (from this cycle's _reconcile_step
        snapshot) must always be passed back in, or moving the stop would
        silently strip the TP. tp_price=None means the position genuinely
        has no TP (never the case for S007 orders, which always place one --
        see _place_market_step), tolerated here so a manually-edited
        position can't crash the cycle.

        `digits`: symbol price precision -- same rounding contract as
        _place_market_step (cTrader rejects more decimals than the symbol
        allows, found live 2026-08-06)."""
        req = ProtoOAAmendPositionSLTPReq()
        req.ctidTraderAccountId = self.account
        req.positionId = position_id
        req.stopLoss = round(float(sl_price), digits)
        if tp_price:
            req.takeProfit = round(float(tp_price), digits)
        d = self.client.send(req)
        d.addCallback(self._check_response)
        return d

    def run_live_cycle(self, symbol_candidates, history_days: int, decide):
        """One connect/auth/work/disconnect session for a full bot cycle.

        `symbol_candidates`: ALGODEV-31: as of webapp/runner.py::_worker_s007,
        this is normally bot/symbol_resolver.py::resolve_symbol()'s output --
        a single verified ticker once a webapp.models.BrokerAssetSymbol row
        exists for the account's broker, or bot/s007_config.py's
        SYMBOL_CANDIDATES guess-list as a logged fallback otherwise. Either
        way the matching below against THIS broker's actual live symbol list
        is the real safety net (a symbol can be renamed/delisted after being
        verified) and is deliberately unchanged by that ticket.

        Resolves the symbol, fetches the instrument's contract metadata (for
        risk sizing) and the account balance, gets M1 bars, lists open
        positions, then calls
          decide(symbol, m1, positions, balance, money_per_point_per_lot,
                 closed_deals=...)
            -> list[action]
        (pure Python, no I/O -- balance and money_per_point_per_lot are
        fetched here, once per cycle, precisely so `decide` doesn't have to
        make its own broker calls) where each action is
          {"kind": "place", side, sl, tp, volume_lots, label, ...},
          {"kind": "close", position_id, volume, label, ...}  or
          {"kind": "amend", position_id, sl, tp, label, ...}   (move SL,
              keep TP -- ALGODEV-37 live breakeven; tp MUST carry the
              position's current takeProfit, see _amend_position_sltp_step)
        and executes the actions in order. Returns
          {"symbol", "m1", "positions", "actions", "results", "balance",
           "money_per_point_per_lot", "timings", "action_timings"}
        where results[i] = {"action", "result", "error"} lines up with actions.

        timings (ALGODEV-44, added 2026-09-15, parallelized 2026-09-17):
        {step_name: duration_ms} for every step above (connect_ms/
        app_auth_ms/account_auth_ms come from the same session's
        CTraderAdapter._run, plus load_symbols_ms/decide_ms/actions_total_ms/
        post_reconcile_ms). full_symbol_ms/balance_ms/m1_ms/reconcile_ms/
        deal_list_ms are fired concurrently via defer.gatherResults (Phase 1
        -- confirmed independent of each other, only of _load_symbols()) and
        each records its OWN elapsed time, so they overlap rather than sum;
        parallel_read_ms is the wall-clock cost of that whole block, for
        comparing against the old sequential total (sum of the five) to see
        the actual saving. action_timings is a parallel per-action list
        ({kind, label, duration_ms}), since a cycle's action count/labels
        vary run to run -- actions themselves are still placed sequentially
        (see the loop below), not parallelized.
        """
        def work(done):
            @defer.inlineCallbacks
            def flow():
                # ALGODEV-44: per-step wall-clock timings, to find out where
                # the reported up-to-20s cycle time actually goes (network
                # round trips, the SDK's own send-queue throttle, or decide()
                # itself) -- Phase 0. Confirmed live (2026-09-16/17) that the
                # SDK's send-queue throttle, not decide() or real network
                # latency, dominates -- see the parallel-reads block below
                # (Phase 1) for the fix that follows from that. `t` is reset
                # after each SEQUENTIALLY measured step; self._session_timings
                # (connect/app-auth/account-auth, populated by
                # CTraderAdapter._run before flow() ever starts) is merged in
                # at the end so callers get one complete picture.
                timings: dict = {}
                t = time.monotonic()

                yield self._load_symbols()
                timings["load_symbols_ms"] = round((time.monotonic() - t) * 1000, 1)
                t = time.monotonic()

                up = {n.upper() for n in self._symbols.keys()}
                symbol = None
                for c in symbol_candidates:
                    if c.upper() in up:
                        symbol = c
                        break
                if symbol is None:
                    raise RuntimeError(
                        f"none of {symbol_candidates} found; broker symbols "
                        f"e.g. {sorted(self._symbols.keys())[:15]}")

                # ALGODEV-44 Phase 1: the five reads below (contract metadata,
                # balance, M1 bars, open positions, closing deals) depend only
                # on _load_symbols() above, not on each other -- confirmed by
                # live timings (2026-09-16/17): each cost ~0.75-1.2s fired
                # sequentially, dominated by the ctrader-open-api SDK's own
                # send-queue throttle (TcpProtocol._sendStrings, a 1s
                # LoopingCall flushing up to 5 queued messages -- see
                # client.py/tcpProtocol.py in the installed SDK), not real
                # network/server latency (a plain balance query and a full M1
                # history fetch cost almost the same). Firing them together
                # via gatherResults lets the SDK flush most of them in the
                # SAME 1s tick instead of one tick each -- ~6s of sequential
                # reads measured live collapsing toward ~1-2s. `_timed` records
                # each one's OWN elapsed time (not shared/reset like the
                # `t`-based timings elsewhere in this method), since they now
                # resolve concurrently, not in sequence.
                def _timed(d, key):
                    t0 = time.monotonic()

                    def record(result):
                        timings[key] = round((time.monotonic() - t0) * 1000, 1)
                        return result
                    d.addCallback(record)
                    return d

                now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
                # Closing deals over the last 24h: the only place a position
                # that opened AND closed between two reconcile snapshots still
                # exists, with its real fill price. decide() matches them to
                # its own opened-today labels by position_id for the
                # day-level risk budget (see bot/s007_paper.py::decide,
                # 2026-09-02 incident) -- a fetch failure must not kill the
                # trading cycle, so fall back to [] (decide then uses its own
                # logged planned-entry risk). The errback is attached BEFORE
                # this joins the gatherResults call below so a deal-list
                # failure can never fail the other four reads (consumeErrors
                # only protects against an UNCONSUMED failure spamming
                # "Unhandled error in Deferred" -- it does not add
                # per-deferred fallback values on its own).
                d_deal_list = self._deal_list_step(now_ms - 24 * 3600 * 1000, now_ms)
                d_deal_list.addErrback(lambda f: [])

                # Fetched once per cycle (not per order) -- see _place_market_step's
                # full_symbol param and bot/risk.py for how these two feed sizing.
                #
                # KNOWN GAP (documented, not fixed): unlike bot/s009_paper.py's
                # drop_forming, nothing here excludes the most-recently-closed
                # M1 bar even when the cycle queries it only seconds after it
                # closed -- entry detection (strategies/ger40_lonfra/setups.py
                # ::find_setup) trusts that bar's close at face value. Found
                # live 2026-08-31 10:03 Kyiv: a B-down setup's live order used
                # entry=26440.6 (from the bar closed 4s earlier) with
                # tp=26442.6 -- cTrader rejected it (TRADING_BAD_STOPS: TP
                # must be < entry on a SELL). The 10:05 cycle re-scanned with
                # 2 more bars available and resolved the SAME setup as
                # already-tp'd at entry=26469.1 -- a different bar than the
                # one used 2 minutes earlier, consistent with that bar's
                # close not having fully settled broker-side at query time.
                # Even a perfect fix here likely wouldn't have caught this
                # specific trade live: by 10:05 the whole move (entry->TP)
                # had already happened within already-elapsed bars -- same
                # "too fast for a 1-minute poll" class as the ghost-trade
                # reasoning behind promoting WORKING_S007_LIQFLOOR (see that
                # preset's own comment, decisions-log.md 2026-08-11/12). No
                # code changed for this -- touching find_setup's bar window
                # is a signal-engine behavior change and needs backtest
                # validation (Gate 0/1) first, not a quick live patch.
                t_parallel = time.monotonic()
                try:
                    full_symbol, balance, m1, positions, closed_deals = yield defer.gatherResults(
                        [_timed(self._get_full_symbol_step(symbol), "full_symbol_ms"),
                         _timed(self._get_balance_step(), "balance_ms"),
                         _timed(self._get_m1_step(symbol, history_days), "m1_ms"),
                         _timed(self._reconcile_step(), "reconcile_ms"),
                         _timed(d_deal_list, "deal_list_ms")],
                        consumeErrors=True)
                except defer.FirstError as fe:
                    # Unwrap: a bare FirstError hides which of the 4 required
                    # reads actually failed and why (deal_list can't reach
                    # here, its own errback above already turned failure into
                    # a successful [] -- so a FirstError only ever means one
                    # of full_symbol/balance/m1/reconcile genuinely failed).
                    raise fe.subFailure.value from fe.subFailure.value
                timings["parallel_read_ms"] = round((time.monotonic() - t_parallel) * 1000, 1)
                t = time.monotonic()
                # Correct in THIS SYMBOL's own quote currency (EUR for
                # GER40/DE40) -- NOT yet converted to the account's deposit
                # currency. bot/s007_paper.py::decide() applies that
                # conversion (C.EUR_TO_USD_FX_RATE_APPROX) before using this
                # for any risk math; see decisions-log.md 2026-07-23.
                money_per_point_per_lot = full_symbol.lotSize

                actions = decide(symbol, m1, positions, balance, money_per_point_per_lot,
                                 closed_deals=closed_deals)
                # decide() is pure Python (no I/O, see its own docstring) --
                # timed anyway so a slow cycle can be told apart from "the
                # engine itself is slow" vs. "the network/broker is slow"
                # instead of assuming it's always the latter.
                timings["decide_ms"] = round((time.monotonic() - t) * 1000, 1)

                results = []
                placed_ok = False
                action_timings = []
                for a in actions:
                    t_action = time.monotonic()
                    try:
                        if a["kind"] == "place":
                            r = yield self._place_market_step(
                                symbol, a["side"], a["sl"], a["tp"], a["volume_lots"], a["label"],
                                full_symbol=full_symbol)
                            placed_ok = True
                        elif a["kind"] == "amend":
                            r = yield self._amend_position_sltp_step(
                                a["position_id"], a["sl"], a["tp"],
                                digits=full_symbol.digits)
                        else:
                            r = yield self._close_position_step(a["position_id"], a["volume"])
                        results.append(dict(action=a, result=r, error=None))
                    except Exception as e:
                        results.append(dict(action=a, result=None, error=e))
                    action_timings.append(dict(
                        kind=a["kind"], label=a.get("label"),
                        duration_ms=round((time.monotonic() - t_action) * 1000, 1)))
                timings["actions_total_ms"] = round(
                    sum(x["duration_ms"] for x in action_timings), 1)

                # ALGODEV-39: a fresh placement's REAL fill price/stop is only
                # ever visible via reconcile() -- the `positions` snapshot
                # above was fetched BEFORE this cycle's own orders, and the
                # NewOrderReq response only carries positionId (see
                # bot/s007_paper.py's `pid` extraction), not price. One extra
                # reconcile call, same session, only when this cycle actually
                # placed something, so the caller can log/use the broker's
                # real entry immediately instead of waiting a full cycle for
                # `have` to catch up. A fetch failure here must not fail
                # orders that already succeeded -- fall back to the
                # pre-orders snapshot (caller then falls back to its own
                # planned values, same as before this existed).
                post_positions = positions
                t = time.monotonic()
                if placed_ok:
                    try:
                        post_positions = yield self._reconcile_step()
                    except Exception:
                        post_positions = positions
                timings["post_reconcile_ms"] = round((time.monotonic() - t) * 1000, 1)

                # self._session_timings (connect_ms/app_auth_ms/account_auth_ms)
                # was populated by CTraderAdapter._run before flow() started --
                # merge last so a caller sees one flat dict for the whole
                # session, not two.
                timings.update(self._session_timings)

                return dict(symbol=symbol, m1=m1, positions=positions,
                            post_positions=post_positions,
                            actions=actions, results=results, balance=balance,
                            money_per_point_per_lot=money_per_point_per_lot,
                            timings=timings, action_timings=action_timings)

            d = flow()
            d.addCallbacks(lambda r: done(r), lambda f: done(error=f))
        return self._run(work)

    # ---------- ALGODEV-45 step 2: persistent-session observation daemon ----------
    #
    # Everything below is NEW and ADDITIVE for scripts/s007_daemon.py's shadow-
    # only observation loop -- run_live_cycle/_run above are NOT touched by any
    # of this and remain the one and only path that can place/amend/close a
    # real order. Deliberately duplicates (rather than shares/refactors) the
    # small connect+auth bootstrap that CTraderAdapter._run already has: that
    # method is the live trading path for BOTH S007 and S011, real money runs
    # through it every minute, and step 2's own plan (ALGODEV-45) is explicit
    # that this stage must not touch anything already proven and live -- a
    # careful duplication here is the safer trade against a shared refactor of
    # code this sensitive. Promote to CTraderAdapter later if a second
    # persistent-session consumer actually needs it (code-architecture's
    # "generalize when a second consumer needs it", not speculatively).

    def _run_persistent(self, on_ready, on_disconnected=None) -> None:
        """Connect + app-auth + account-auth ONCE, then call `on_ready()` and
        keep the reactor running -- unlike _run(), nothing here ever calls
        reactor.stop() on its own. Blocks the calling thread for as long as
        the reactor runs; call stop_persistent() (e.g. from a signal handler)
        for a clean shutdown, or rely on `on_disconnected` firing if the
        connection drops on its own.

        Step 2 is deliberately conservative about failure: a dropped
        connection calls `on_disconnected` (if given) and then stops the
        reactor outright -- no automatic reconnect. Auto-reconnect is its own
        source of subtle bugs (exactly the kind of thing the ALGODEV-45 plan
        wants proven separately, not bundled in) -- during this observation
        phase a disconnect should be visible and require a human to restart
        the process, not silently paper over itself.
        """
        from twisted.internet import reactor
        self._session_timings = {}
        self._persistent_stopped = False
        t_start = time.monotonic()

        def mark(key: str, t0: float):
            def cb(result):
                self._session_timings[key] = round((time.monotonic() - t0) * 1000, 1)
                return result
            return cb

        def _on_disconnected(_client, reason):
            if self._persistent_stopped:
                return
            if on_disconnected:
                on_disconnected(reason)
            self.stop_persistent()

        def on_connected(_client):
            self._session_timings["connect_ms"] = round((time.monotonic() - t_start) * 1000, 1)
            t_app_auth = time.monotonic()
            req = ProtoOAApplicationAuthReq()
            req.clientId = self.client_id
            req.clientSecret = self.secret
            d = self.client.send(req)
            d.addCallback(mark("app_auth_ms", t_app_auth))
            t_account_auth = time.monotonic()
            d.addCallback(lambda _r: self._auth_account())
            d.addCallback(mark("account_auth_ms", t_account_auth))
            d.addCallback(lambda _r: on_ready())
            d.addErrback(lambda f: (on_disconnected(f) if on_disconnected else None,
                                    self.stop_persistent()))

        self.client.setConnectedCallback(on_connected)
        self.client.setDisconnectedCallback(_on_disconnected)
        self.client.startService()
        reactor.run(installSignalHandlers=False)

    def stop_persistent(self) -> None:
        """Clean shutdown for _run_persistent -- safe to call more than once
        (e.g. once from a signal handler and once from a disconnect callback
        racing it) and safe to call from any thread."""
        from twisted.internet import reactor
        if self._persistent_stopped:
            return
        self._persistent_stopped = True
        try:
            self.client.stopService()
        except Exception:
            pass
        if reactor.running:
            reactor.callFromThread(reactor.stop)

    @defer.inlineCallbacks
    def shadow_tick_step(self, symbol_candidates, history_days: int):
        """One observation tick on an ALREADY-open persistent session
        (call only from inside _run_persistent's on_ready/LoopingCall, never
        standalone). Fetches exactly what a real cycle would -- symbol
        resolution, contract metadata, balance, M1 bars, open positions,
        closing deals, the same ALGODEV-44 Phase 1 parallel read -- but never
        calls decide() and never places/amends/closes anything. There is no
        code path from here to any order-sending method.

        This step is about proving the PERSISTENT-SESSION MECHANICS hold up
        (does reusing one connection for these reads work correctly and
        indefinitely, across hours, without hanging) -- NOT about
        re-validating the trading logic itself, which is already exercised
        live via the untouched run_live_cycle path above. Returns a small
        summary dict for the daemon to log each tick.

        timings (added 2026-09-21, Anton: "есть логи сколько времени
        занимают запросы"): {step_name: duration_ms}, same shape/fields as
        run_live_cycle's (load_symbols_ms/full_symbol_ms/balance_ms/m1_ms/
        reconcile_ms/deal_list_ms/parallel_read_ms) MINUS connect_ms/
        app_auth_ms/account_auth_ms -- this call never pays those, that's
        the entire point of a persistent session (paid once at daemon
        startup, not per tick). Comparing these numbers against the
        cold-connect-per-cycle baseline (ALGODEV-44 Phase 0/1, ~0.75-1.2s
        per read) is exactly the data point this step exists to produce.
        """
        timings: dict = {}
        t = time.monotonic()
        yield self._load_symbols()
        timings["load_symbols_ms"] = round((time.monotonic() - t) * 1000, 1)

        up = {n.upper() for n in self._symbols.keys()}
        symbol = next((c for c in symbol_candidates if c.upper() in up), None)
        if symbol is None:
            raise RuntimeError(
                f"none of {symbol_candidates} found; broker symbols "
                f"e.g. {sorted(self._symbols.keys())[:15]}")

        def _timed(d, key):
            t0 = time.monotonic()

            def record(result):
                timings[key] = round((time.monotonic() - t0) * 1000, 1)
                return result
            d.addCallback(record)
            return d

        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        d_deal_list = self._deal_list_step(now_ms - 24 * 3600 * 1000, now_ms)
        d_deal_list.addErrback(lambda f: [])

        t_parallel = time.monotonic()
        try:
            full_symbol, balance, m1, positions, closed_deals = yield defer.gatherResults(
                [_timed(self._get_full_symbol_step(symbol), "full_symbol_ms"),
                 _timed(self._get_balance_step(), "balance_ms"),
                 _timed(self._get_m1_step(symbol, history_days), "m1_ms"),
                 _timed(self._reconcile_step(), "reconcile_ms"),
                 _timed(d_deal_list, "deal_list_ms")],
                consumeErrors=True)
        except defer.FirstError as fe:
            raise fe.subFailure.value from fe.subFailure.value
        timings["parallel_read_ms"] = round((time.monotonic() - t_parallel) * 1000, 1)

        return dict(symbol=symbol, balance=balance,
                    n_bars=len(m1), last_bar=str(m1.index[-1]) if len(m1) else None,
                    n_positions=len(positions), n_closed_deals=len(closed_deals),
                    lot_size=full_symbol.lotSize, timings=timings)
