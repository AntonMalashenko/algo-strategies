"""cTrader adapter extension for S007 — adds M1 bars, MARKET orders and position
close on top of the S004 `CTraderAdapter` (same connection/auth/credentials).

Reuses everything from bot.ctrader; only the pieces S007 needs that S004 didn't
(index instrument, market entries, pyramiding, position close) are added here.

NOTE (as in bot/ctrader.py): field/enum names follow the Open API spec; a couple
of order/price details can differ by SDK version and broker — run `--check` and
`--dry-run` first, then verify the first few live orders against the cTrader UI.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd

from bot.ctrader import CTraderAdapter, HAVE_SDK, Protobuf  # reuse S004 adapter

if HAVE_SDK:
    from twisted.internet import defer
    from ctrader_open_api import Client, TcpProtocol, EndPoints
    from ctrader_open_api.messages.OpenApiMessages_pb2 import (
        ProtoOAGetTrendbarsReq, ProtoOANewOrderReq, ProtoOAClosePositionReq,
        ProtoOAReconcileReq, ProtoOASymbolByIdReq,
        ProtoOAOrderErrorEvent, ProtoOAErrorRes,
    )
    from ctrader_open_api.messages.OpenApiModelMessages_pb2 import (
        ProtoOAOrderType, ProtoOATradeSide, ProtoOATrendbarPeriod,
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
                ))
            return out
        d.addCallback(fin)
        return d

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
            req.stopLoss = float(sl_price)
            req.takeProfit = float(tp_price)
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

    def run_live_cycle(self, symbol_candidates, history_days: int, decide):
        """One connect/auth/work/disconnect session for a full bot cycle.

        Resolves the symbol, fetches the instrument's contract metadata (for
        risk sizing) and the account balance, gets M1 bars, lists open
        positions, then calls
          decide(symbol, m1, positions, balance, money_per_point_per_lot)
            -> list[action]
        (pure Python, no I/O -- balance and money_per_point_per_lot are
        fetched here, once per cycle, precisely so `decide` doesn't have to
        make its own broker calls) where each action is
          {"kind": "place", side, sl, tp, volume_lots, label, ...}  or
          {"kind": "close", position_id, volume, label, ...}
        and executes the actions in order. Returns
          {"symbol", "m1", "positions", "actions", "results", "balance",
           "money_per_point_per_lot"}
        where results[i] = {"action", "result", "error"} lines up with actions.
        """
        def work(done):
            @defer.inlineCallbacks
            def flow():
                yield self._load_symbols()
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

                # Fetched once per cycle (not per order) -- see _place_market_step's
                # full_symbol param and bot/risk.py for how these two feed sizing.
                full_symbol = yield self._get_full_symbol_step(symbol)
                balance = yield self._get_balance_step()
                # Correct in THIS SYMBOL's own quote currency (EUR for
                # GER40/DE40) -- NOT yet converted to the account's deposit
                # currency. bot/s007_paper.py::decide() applies that
                # conversion (C.EUR_TO_USD_FX_RATE_APPROX) before using this
                # for any risk math; see decisions-log.md 2026-07-23.
                money_per_point_per_lot = full_symbol.lotSize

                m1 = yield self._get_m1_step(symbol, history_days)
                positions = yield self._reconcile_step()
                actions = decide(symbol, m1, positions, balance, money_per_point_per_lot)

                results = []
                for a in actions:
                    try:
                        if a["kind"] == "place":
                            r = yield self._place_market_step(
                                symbol, a["side"], a["sl"], a["tp"], a["volume_lots"], a["label"],
                                full_symbol=full_symbol)
                        else:
                            r = yield self._close_position_step(a["position_id"], a["volume"])
                        results.append(dict(action=a, result=r, error=None))
                    except Exception as e:
                        results.append(dict(action=a, result=None, error=e))

                return dict(symbol=symbol, m1=m1, positions=positions,
                            actions=actions, results=results, balance=balance,
                            money_per_point_per_lot=money_per_point_per_lot)

            d = flow()
            d.addCallbacks(lambda r: done(r), lambda f: done(error=f))
        return self._run(work)
