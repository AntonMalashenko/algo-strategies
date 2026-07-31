"""cTrader Open API adapter (Spotware OpenApiPy, protobuf over TLS).

Install on the trading machine:

    pip install ctrader-open-api

Credentials come from <repo>/.env via bot.config. The adapter runs one
"session" per call cycle inside the twisted reactor: connect -> app auth ->
account auth -> perform queued work -> stop reactor. This fits the bot's
run-every-15-minutes model (no long-lived process needed).

Verify credentials with:  python -m bot.paper --check

NOTE: message/field names follow the official Open API spec; small
adjustments may be needed against the installed SDK version — debug
interactively on first run.
"""
from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

import pandas as pd

from bot import config as C

try:
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
except ImportError:            # dev machines without the SDK
    HAVE_SDK = False


class CTraderAdapter:
    """Synchronous facade over the async SDK: each public method queues the
    work and runs the reactor until done. One instance per bot cycle."""

    def __init__(self, require_account: bool = True):
        if not HAVE_SDK:
            raise RuntimeError("pip install ctrader-open-api first")
        creds = C.ctrader_credentials()
        self.client_id = creds["client_id"]
        self.secret = creds["client_secret"]
        self.token = creds["access_token"]
        self.account = int(creds["account_id"] or 0)
        host = creds["host"] or EndPoints.PROTOBUF_DEMO_HOST
        need = [self.client_id, self.secret, self.token]
        if require_account:
            need.append(self.account)
        if not all(need):
            raise RuntimeError(
                "missing CTRADER_* credentials (see configs/accounts.yml / bot/config.py)")
        self.client = Client(host, EndPoints.PROTOBUF_PORT, TcpProtocol)
        self._result = None
        self._error = None
        self._symbols = None            # NAME -> light symbol

    # ---------- session plumbing ----------

    def _run(self, work, auth_account: bool = True):
        """Connect, authenticate, run `work(done)`; block until finished.

        auth_account=False stops after application auth — used to fetch the
        account list before the account id is known."""
        from twisted.internet import reactor

        self._result, self._error = None, None
        finished = threading.Event()

        def done(result=None, error=None):
            if finished.is_set():
                return
            self._result, self._error = result, error
            finished.set()
            try:
                self.client.stopService()
            except Exception:
                pass
            if reactor.running:
                reactor.callFromThread(reactor.stop)

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
            lambda _c, reason: done(error=reason) if not finished.is_set() else None)
        self.client.startService()
        reactor.run(installSignalHandlers=False)
        if self._error is not None:
            raise RuntimeError(f"cTrader error: {self._error}")
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

    def _get_balance_step(self):
        """Account balance in deposit currency (session-chainable -- assumes
        an already-connected+authed session, like `_get_m1_step` in
        ctrader_s007.py; see `run_live_cycle`). Used to compute equal-
        dollar-risk position sizing fresh every cycle, since balance moves
        with every fill."""
        req = ProtoOATraderReq()
        req.ctidTraderAccountId = self.account
        d = self.client.send(req)
        d.addCallback(lambda resp: Protobuf.extract(resp).trader.balance / 100.0)
        return d

    def check(self) -> dict:
        """Connectivity check: auth + balance + our pairs present."""
        def work(done):
            d = self._load_symbols()
            d.addCallback(lambda _m: self._get_balance_step())

            def fin(balance):
                done(dict(
                    balance=balance,
                    symbols_found=[p for p in C.PAIRS if p.upper() in self._symbols],
                ))
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
