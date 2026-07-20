"""cTrader Open API adapter (Spotware OpenApiPy, protobuf over TLS).

Install on the trading machine:

    pip install ctrader-open-api

Credentials come from <repo>/.env via bot.config. The adapter runs one
"session" per call inside the twisted reactor: connect -> app auth ->
account auth -> perform queued work -> disconnect. A bot cycle (e.g. S007's
--live) typically does several of these calls back to back (resolve symbol,
get M1 bars, list positions, place/close orders); crochet keeps a single
reactor alive in a background thread for the whole process so each call can
block synchronously without hitting Twisted's "reactor can only run once"
restriction.

Verify credentials with:  python -m bot.paper --check

NOTE: message/field names follow the official Open API spec; small
adjustments may be needed against the installed SDK version — debug
interactively on first run.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd

from bot import config as C

try:
    import crochet
    crochet.setup()   # idempotent; starts the reactor in a background thread once
    from ctrader_open_api import Client, Protobuf, TcpProtocol, EndPoints
    from ctrader_open_api.messages.OpenApiMessages_pb2 import (
        ProtoOAApplicationAuthReq, ProtoOAAccountAuthReq,
        ProtoOAGetTrendbarsReq, ProtoOASymbolsListReq,
        ProtoOANewOrderReq, ProtoOACancelOrderReq, ProtoOAReconcileReq,
        ProtoOATraderReq, ProtoOAGetAccountListByAccessTokenReq,
    )
    from ctrader_open_api.messages.OpenApiModelMessages_pb2 import (
        ProtoOAOrderType, ProtoOATradeSide, ProtoOATrendbarPeriod,
    )
    HAVE_SDK = True
except ImportError:            # dev machines without the SDK (or crochet)
    HAVE_SDK = False

CALL_TIMEOUT = 30   # seconds; one connect+auth+work+disconnect cycle


class CTraderAdapter:
    """Synchronous facade over the async SDK: each public method queues the
    work and runs the reactor until done. One instance per bot cycle."""

    def __init__(self, require_account: bool = True):
        if not HAVE_SDK:
            raise RuntimeError("pip install ctrader-open-api first")
        self.client_id = C.cred("CTRADER_CLIENT_ID")
        self.secret = C.cred("CTRADER_CLIENT_SECRET")
        self.token = C.cred("CTRADER_ACCESS_TOKEN")
        self.account = int(C.cred("CTRADER_ACCOUNT_ID", "0"))
        host = C.cred("CTRADER_HOST") or EndPoints.PROTOBUF_DEMO_HOST
        need = [self.client_id, self.secret, self.token]
        if require_account:
            need.append(self.account)
        if not all(need):
            raise RuntimeError("missing CTRADER_* credentials (see bot/config.py)")
        self.client = Client(host, EndPoints.PROTOBUF_PORT, TcpProtocol)
        self._result = None
        self._error = None
        self._symbols = None            # NAME -> light symbol

    # ---------- session plumbing ----------

    def _run(self, work, auth_account: bool = True):
        """Connect, authenticate, run `work(done)`; block until finished.

        auth_account=False stops after application auth — used to fetch the
        account list before the account id is known.

        Runs on crochet's persistent background reactor thread (see module
        docstring), so this may be called many times per process — each call
        opens its own connection, does its work, and disconnects."""
        from twisted.internet.defer import Deferred

        @crochet.wait_for(timeout=CALL_TIMEOUT)
        def _do():
            result_d = Deferred()

            def done(result=None, error=None):
                if result_d.called:
                    return
                try:
                    self.client.stopService()
                except Exception:
                    pass
                if error is not None:
                    result_d.errback(error)
                else:
                    result_d.callback(result)

            def on_connected(_client):
                req = ProtoOAApplicationAuthReq()
                req.clientId = self.client_id
                req.clientSecret = self.secret
                d = self.client.send(req)
                if auth_account:
                    d.addCallback(lambda _r: self._auth_account())
                d.addCallback(lambda _r: work(done))
                d.addErrback(lambda f: done(error=f))

            self.client.setConnectedCallback(on_connected)
            self.client.setDisconnectedCallback(
                lambda _c, reason: done(error=reason) if not result_d.called else None)
            self.client.startService()
            return result_d

        self._result, self._error = None, None
        try:
            self._result = _do()
        except Exception as e:
            self._error = e
            raise RuntimeError(f"cTrader error: {e}") from e
        return self._result

    def _auth_account(self):
        req = ProtoOAAccountAuthReq()
        req.ctidTraderAccountId = self.account
        req.accessToken = self.token
        return self.client.send(req)

    def _load_symbols(self):
        req = ProtoOASymbolsListReq()
        req.ctidTraderAccountId = self.account
        d = self.client.send(req)

        def store(resp):
            msg = Protobuf.extract(resp)
            self._symbols = {s.symbolName.upper(): s for s in msg.symbol}
            return msg
        d.addCallback(store)
        return d

    def symbol_id(self, name: str) -> int:
        return self._symbols[name.upper()].symbolId

    # ---------- public operations ----------

    def get_accounts(self) -> list:
        """Account list authorised by the access token (needs only app auth).
        Returns dicts with ctidTraderAccountId and isLive."""
        def work(done):
            req = ProtoOAGetAccountListByAccessTokenReq()
            req.accessToken = self.token
            d = self.client.send(req)

            def fin(resp):
                msg = Protobuf.extract(resp)
                done([dict(account_id=a.ctidTraderAccountId,
                           is_live=a.isLive) for a in msg.ctidTraderAccount])
            d.addCallbacks(fin, lambda f: done(error=f))
        return self._run(work, auth_account=False)

    def check(self) -> dict:
        """Connectivity check: auth + balance + our pairs present."""
        def work(done):
            d = self._load_symbols()

            def ask_trader(_msg):
                req = ProtoOATraderReq()
                req.ctidTraderAccountId = self.account
                return self.client.send(req)

            def fin(resp):
                t = Protobuf.extract(resp).trader
                done(dict(
                    balance=t.balance / 100.0,
                    symbols_found=[p for p in C.PAIRS if p.upper() in self._symbols],
                ))
            d.addCallback(ask_trader)
            d.addCallbacks(fin, lambda f: done(error=f))
        return self._run(work)

    def get_m15(self, symbol: str, days: int) -> pd.DataFrame:
        """M15 bars in RAW POINTS (pip = 10 raw), naive EET index —
        the exact convention the engine was validated on."""
        def work(done):
            d = self._load_symbols()

            def ask_bars(_msg):
                req = ProtoOAGetTrendbarsReq()
                req.ctidTraderAccountId = self.account
                req.symbolId = self.symbol_id(symbol)
                req.period = ProtoOATrendbarPeriod.M15
                now = datetime.now(timezone.utc)
                req.fromTimestamp = int((now - timedelta(days=days)).timestamp() * 1000)
                req.toTimestamp = int(now.timestamp() * 1000)
                return self.client.send(req)

            def fin(resp):
                msg = Protobuf.extract(resp)
                sym = self._symbols[symbol.upper()]
                digits = getattr(sym, "digits", 5)
                # Open API bar prices are ints = human_price * 1e5.
                # Our engine raw points = human_price * 10^digits
                # (EURUSD 1.08423 -> 108423; USDJPY 145.323 -> 145323),
                # so raw = api_int / 10^(5 - digits).
                div = 10 ** (5 - digits)
                rows = []
                for tb in msg.trendbar:
                    lo = tb.low
                    rows.append(dict(
                        ts=tb.utcTimestampInMinutes * 60,
                        open=(lo + tb.deltaOpen) / div,
                        high=(lo + tb.deltaHigh) / div,
                        low=lo / div,
                        close=(lo + tb.deltaClose) / div,
                        volume=tb.volume,
                    ))
                df = pd.DataFrame(rows)
                ts = (pd.to_datetime(df.pop("ts"), unit="s", utc=True)
                      .dt.tz_convert("Europe/Bucharest").dt.tz_localize(None))
                df.index = ts
                done(df.sort_index())
            d.addCallback(ask_bars)
            d.addCallbacks(fin, lambda f: done(error=f))
        return self._run(work)

    def reconcile(self) -> dict:
        """Open positions and pending orders on the account."""
        def work(done):
            req = ProtoOAReconcileReq()
            req.ctidTraderAccountId = self.account
            d = self.client.send(req)

            def fin(resp):
                msg = Protobuf.extract(resp)
                done(dict(positions=list(msg.position), orders=list(msg.order)))
            d.addCallbacks(fin, lambda f: done(error=f))
        return self._run(work)

    def place_limit(self, symbol: str, side: str, price_raw: float,
                    sl_raw: float, tp_raw: float, volume_lots: float,
                    label: str):
        """Limit order with attached SL/TP. price args in engine raw points."""
        def work(done):
            d = self._load_symbols()

            def send_order(_msg):
                sym = self._symbols[symbol.upper()]
                digits = getattr(sym, "digits", 5)
                req = ProtoOANewOrderReq()
                req.ctidTraderAccountId = self.account
                req.symbolId = sym.symbolId
                req.orderType = ProtoOAOrderType.LIMIT
                req.tradeSide = (ProtoOATradeSide.BUY if side == "buy"
                                 else ProtoOATradeSide.SELL)
                # volume in cents of units: 0.01 lot = 1000 units = 100000
                req.volume = int(round(volume_lots * 100000 * 100))
                # engine raw points -> HUMAN price (raw = human * 10^digits);
                # NewOrderReq takes human doubles per the Open API spec.
                # Verify on the first live run against the cTrader UI.
                req.limitPrice = price_raw / (10 ** digits)
                req.stopLoss = sl_raw / (10 ** digits)
                req.takeProfit = tp_raw / (10 ** digits)
                req.label = label
                req.comment = "S004 paper"
                return self.client.send(req)
            d.addCallback(send_order)
            d.addCallbacks(lambda r: done(r), lambda f: done(error=f))
        return self._run(work)

    def cancel(self, order_id: int):
        def work(done):
            req = ProtoOACancelOrderReq()
            req.ctidTraderAccountId = self.account
            req.orderId = order_id
            d = self.client.send(req)
            d.addCallbacks(lambda r: done(r), lambda f: done(error=f))
        return self._run(work)
