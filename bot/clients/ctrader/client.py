"""Standalone cTrader Open API client (Spotware OpenApiPy, protobuf over TLS).

Install on the trading machine:

    pip install ctrader-open-api

What this is
------------
`CTraderApiClient` is a clean, self-contained synchronous facade over the
async Spotware SDK: it opens its OWN session per call (connect -> application
auth -> account auth -> run the queued work -> stop the reactor) and sends its
own protobuf requests. It exposes the broker operations the bot actually uses
(connectivity check, accounts, balance, symbols, trendbars, reconcile, deals,
market/limit orders, close, amend SL/TP, cancel) as plain Python methods with
human prices and plain dicts.

Accepted duplication
--------------------
The ~60 lines of session plumbing below (`_run`, `_auth_account`,
`_load_symbols`, `_check_response`) are a DELIBERATE duplicate of
`bot/ctrader.py` / `bot/ctrader_s007.py`, accepted by the maintainer: this
module is the clean interface future code will migrate onto, and it must not
depend on the legacy per-strategy adapter hierarchy while that migration is
pending. Nothing imports it yet — it is not wired into any strategy, runner or
webapp path.

Dev machines
------------
The SDK import is guarded by `HAVE_SDK` (same pattern as `bot/ctrader.py`), so
this module imports cleanly where `ctrader-open-api` is not installed;
constructing the client there raises `RuntimeError`.

NOTE: message/enum/field names follow the official Open API spec. Small
adjustments can be needed against a given SDK version or broker — verify the
first live orders and volumes against the cTrader UI before trusting them.
"""
from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

import pandas as pd

from bot.clients.base.base import BaseClient

try:
    from twisted.internet import defer
    from ctrader_open_api import Client, Protobuf, TcpProtocol, EndPoints
    from ctrader_open_api.messages.OpenApiMessages_pb2 import (
        ProtoOAApplicationAuthReq, ProtoOAAccountAuthReq,
        ProtoOAGetTrendbarsReq, ProtoOASymbolsListReq, ProtoOASymbolByIdReq,
        ProtoOAReconcileReq, ProtoOATraderReq,
        ProtoOAGetAccountListByAccessTokenReq,
        ProtoOANewOrderReq, ProtoOAClosePositionReq, ProtoOACancelOrderReq,
        ProtoOAAmendPositionSLTPReq, ProtoOADealListReq,
        ProtoOAOrderErrorEvent, ProtoOAErrorRes,
    )
    from ctrader_open_api.messages.OpenApiModelMessages_pb2 import (
        ProtoOAOrderType, ProtoOATradeSide, ProtoOATrendbarPeriod,
        ProtoOADealStatus,
    )
    HAVE_SDK = True
except ImportError:            # dev machines without the SDK
    HAVE_SDK = False

# ---------------------------------------------------------------------------
# Protocol constants (fixed by the Open API, not tunables)
# ---------------------------------------------------------------------------

PRICE_SCALE = 100_000.0        # Open API trendbar/price integers = human_price * 1e5
DEAL_WINDOW_DAYS = 7           # cTrader rejects a ProtoOADealListReq spanning more than a week
VOLUME_LOT_UNIT = 100          # Open API volume is 1/100-lot units: raw = lots * VOLUME_LOT_UNIT * lotSize
BALANCE_SCALE = 100.0          # ProtoOATrader.balance is in 1/100 units of the deposit currency
DEFAULT_PRICE_DIGITS = 5       # fallback price precision when a symbol carries no `digits`
DEFAULT_MONEY_DIGITS = 2       # fallback money scale for deal PnL (money = int / 10^moneyDigits)
DEFAULT_AMEND_PRICE_DIGITS = 2  # SL/TP rounding used by amend when the caller knows no better
DEFAULT_DEAL_MAX_ROWS = 1000   # ProtoOADealListReq maxRows cap, per window
DEFAULT_VOLUME_STEP = 1        # fallback stepVolume when the symbol reports none
MIN_DEAL_LOOKBACK_DAYS = 1     # a deal lookback shorter than this is pointless; floor it
MS_PER_SECOND = 1000           # Open API timestamps are epoch milliseconds
SECONDS_PER_MINUTE = 60        # trendbar.utcTimestampInMinutes -> epoch seconds
SYMBOL_SAMPLE_LIMIT = 15       # how many broker symbol names to quote in a "not found" error

SIDE_BUY = "buy"               # canonical side names used across this client's dicts
SIDE_SELL = "sell"

# Named error messages the broker answers with instead of errbacking; see
# `_check_response`. Compared by class NAME because Protobuf.extract() may
# hand back a class object from a different import path than ours.
ERROR_RES_NAME = "ProtoOAErrorRes"
ORDER_ERROR_EVENT_NAME = "ProtoOAOrderErrorEvent"
ERROR_MESSAGE_NAMES = (ERROR_RES_NAME, ORDER_ERROR_EVENT_NAME)

# Trendbar period name -> Open API enum. Empty without the SDK so the module
# still imports on a dev machine (the constructor raises there anyway).
TRENDBAR_PERIODS = {
    "M1": ProtoOATrendbarPeriod.M1,
    "M5": ProtoOATrendbarPeriod.M5,
    "M15": ProtoOATrendbarPeriod.M15,
    "M30": ProtoOATrendbarPeriod.M30,
    "H1": ProtoOATrendbarPeriod.H1,
    "H4": ProtoOATrendbarPeriod.H4,
    "D1": ProtoOATrendbarPeriod.D1,
} if HAVE_SDK else {}


class CTraderApiClient(BaseClient):
    """Synchronous cTrader Open API client: one session per public call.

    Constructed from a single credentials dict (see `BaseClient`):
    {client_id, client_secret, access_token, account_id, host?,
     require_account?}.
    """

    def __init__(self, creds: dict):
        super().__init__(creds)
        if not HAVE_SDK:
            raise RuntimeError("pip install ctrader-open-api first")
        # .get, not [] -- a wholly absent key is exactly the case the
        # "missing cTrader credentials: ..." message below exists to name.
        self.client_id = creds.get("client_id")
        self.client_secret = creds.get("client_secret")
        self.access_token = creds.get("access_token")
        self.account_id = int(creds.get("account_id") or 0)
        host = creds.get("host") or EndPoints.PROTOBUF_DEMO_HOST
        # require_account=False is for the pre-account bootstrap: listing the
        # accounts a token is authorised for needs application auth only.
        require_account = creds.get("require_account", True)
        missing = []
        if not self.client_id:
            missing.append("client_id")
        if not self.client_secret:
            missing.append("client_secret")
        if not self.access_token:
            missing.append("access_token")
        if require_account and not self.account_id:
            missing.append("account_id")
        if missing:
            raise RuntimeError(f"missing cTrader credentials: {', '.join(missing)}")
        self.client = Client(host, EndPoints.PROTOBUF_PORT, TcpProtocol)
        self._result = None
        self._error = None
        self._symbols = None           # NAME -> ProtoOALightSymbol

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
            req.clientSecret = self.client_secret
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
        """Account auth, with its rejection actually checked.

        An account-auth rejection (expired token, wrong account id, revoked
        access) comes back as a normal ProtoOAErrorRes, not an errback --
        unchecked, every downstream call in the session then fails with a
        misleading "Trading account is not authorized" instead of the real
        reason."""
        req = ProtoOAAccountAuthReq()
        req.ctidTraderAccountId = self.account_id
        req.accessToken = self.access_token
        d = self.client.send(req)

        def check(resp):
            msg = Protobuf.extract(resp)
            if type(msg).__name__ == ERROR_RES_NAME:
                raise RuntimeError(f"account auth failed: {msg.errorCode}: "
                                   f"{getattr(msg, 'description', '')}")
            return resp
        d.addCallback(check)
        return d

    def _load_symbols(self):
        """Populate self._symbols with the account's light symbol list.

        A ProtoOAErrorRes (account-auth expired, session limit, symbol list
        denied) has no `symbol` field -- reading it would crash as an opaque
        AttributeError that hides the broker's own errorCode/description."""
        req = ProtoOASymbolsListReq()
        req.ctidTraderAccountId = self.account_id
        d = self.client.send(req)

        def store(resp):
            msg = Protobuf.extract(resp)
            if type(msg).__name__ == ERROR_RES_NAME:
                raise RuntimeError(f"{msg.errorCode}: {getattr(msg, 'description', '')}")
            self._symbols = {s.symbolName.upper(): s for s in msg.symbol}
            return msg
        d.addCallback(store)
        return d

    def symbol_id(self, name: str) -> int:
        """Broker symbol id for a name; requires symbols loaded in this session."""
        return self._symbols[name.upper()].symbolId

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
        if name in ERROR_MESSAGE_NAMES:
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

    @staticmethod
    def _volume_from_lots(volume_lots: float, full_symbol) -> int:
        """lots -> Open API volume units, clamped to [minVolume, maxVolume]
        and rounded to a stepVolume multiple (a raw lots*100*lotSize can
        otherwise exceed the broker's max, e.g. an FX-sized 0.01 lot is way
        too big for an index CFD with lotSize in the hundreds, not 100000)."""
        step = full_symbol.stepVolume or DEFAULT_VOLUME_STEP
        raw = volume_lots * VOLUME_LOT_UNIT * full_symbol.lotSize
        raw = max(full_symbol.minVolume, min(full_symbol.maxVolume, raw))
        return int(round(raw / step) * step)

    def _full_symbol(self, symbol: str):
        """Full ProtoOASymbol for one name (session-chained deferred).

        ProtoOASymbolsListReq only returns ProtoOALightSymbol (name/id/
        category -- no digits/lotSize/minVolume/maxVolume/stepVolume). Those
        trading params need a separate ProtoOASymbolByIdReq and are required
        to size orders and round prices correctly."""
        req = ProtoOASymbolByIdReq()
        req.ctidTraderAccountId = self.account_id
        req.symbolId.append(self.symbol_id(symbol))
        d = self.client.send(req)
        d.addCallback(self._check_response)
        d.addCallback(lambda msg: msg.symbol[0])
        return d

    @staticmethod
    def _parse_deals(msg) -> list[dict]:
        """Closing deals only: a deal carries `closePositionDetail` exactly
        when it reduced/closed a position, and that sub-message (not the deal
        itself) is where the realised money lives. Opening deals are dropped."""
        filled = (ProtoOADealStatus.FILLED, ProtoOADealStatus.PARTIALLY_FILLED)
        deals = []
        for deal in msg.deal:
            if deal.dealStatus not in filled or not deal.HasField("closePositionDetail"):
                continue
            close_detail = deal.closePositionDetail
            # Money fields are int64 scaled by 10^moneyDigits (moneyDigits is
            # per-message, NOT a constant -- a JPY-deposit account reports a
            # different scale than a USD one), so never hardcode /100 here.
            money_scale = 10.0 ** (close_detail.moneyDigits or deal.moneyDigits
                                   or DEFAULT_MONEY_DIGITS)
            gross_profit = close_detail.grossProfit / money_scale
            swap = close_detail.swap / money_scale
            commission = close_detail.commission / money_scale
            deals.append(dict(
                deal_id=deal.dealId,
                position_id=deal.positionId,
                symbol_id=deal.symbolId,
                side=SIDE_BUY if deal.tradeSide == ProtoOATradeSide.BUY else SIDE_SELL,
                # plain doubles in the Open API, unlike trendbars -- do NOT
                # divide these by PRICE_SCALE.
                exit_price=deal.executionPrice,
                entry_price=close_detail.entryPrice,
                closed_volume=close_detail.closedVolume,
                gross_profit=gross_profit,
                swap=swap,
                commission=commission,
                # swap and commission arrive already signed, so the net is a
                # plain sum, not gross - fees.
                pnl=gross_profit + swap + commission,
                balance_after=close_detail.balance / money_scale,
                executed_ms=deal.executionTimestamp,
            ))
        return deals

    @staticmethod
    def _side_enum(side: str):
        """Canonical side name -> ProtoOATradeSide."""
        return ProtoOATradeSide.BUY if side == SIDE_BUY else ProtoOATradeSide.SELL

    @staticmethod
    def _extract_ids(msg) -> tuple:
        """(position_id, order_id) from an execution event, best effort.

        A ProtoOANewOrderReq is answered with a ProtoOAExecutionEvent whose
        exact populated sub-messages vary by broker and order state, so every
        lookup is a getattr with a None default rather than an assumption."""
        order = getattr(msg, "order", None)
        position = getattr(msg, "position", None)
        order_id = getattr(order, "orderId", None) if order is not None else None
        position_id = getattr(position, "positionId", None) if position is not None else None
        if position_id is None and order is not None:
            position_id = getattr(order, "positionId", None)
        return position_id, order_id

    # ---------- connectivity / account ----------

    def check(self, symbols: list[str] | None = None) -> dict:
        """Connectivity check: auth + balance, and optionally which of
        `symbols` this broker actually offers."""
        def work(done):
            d = self._load_symbols()
            d.addCallback(lambda _msg: self._balance_step())

            def fin(balance):
                if symbols is None:
                    done(dict(balance=balance, symbols_total=len(self._symbols)))
                    return
                found = [name for name in symbols if name.upper() in self._symbols]
                missing = [name for name in symbols if name.upper() not in self._symbols]
                done(dict(balance=balance, symbols_found=found, symbols_missing=missing))
            d.addCallbacks(fin, lambda f: done(error=f))
        return self._run(work)

    def list_accounts(self) -> list[dict]:
        """Accounts the access token is authorised for (application auth only,
        so this works before an account id is known)."""
        def work(done):
            req = ProtoOAGetAccountListByAccessTokenReq()
            req.accessToken = self.access_token
            d = self.client.send(req)

            def fin(resp):
                msg = Protobuf.extract(resp)
                done([dict(account_id=account.ctidTraderAccountId,
                           is_live=account.isLive)
                      for account in msg.ctidTraderAccount])
            d.addCallbacks(fin, lambda f: done(error=f))
        return self._run(work, auth_account=False)

    def _balance_step(self):
        """Account balance in the deposit currency (session-chained deferred)."""
        req = ProtoOATraderReq()
        req.ctidTraderAccountId = self.account_id
        d = self.client.send(req)
        d.addCallback(lambda resp: Protobuf.extract(resp).trader.balance / BALANCE_SCALE)
        return d

    def get_balance(self) -> float:
        """Account balance in the deposit currency."""
        def work(done):
            d = self._balance_step()
            d.addCallbacks(lambda balance: done(balance), lambda f: done(error=f))
        return self._run(work)

    # ---------- symbols ----------

    def list_symbols(self) -> dict[str, int]:
        """{broker symbol name: symbol id} for every symbol on the account."""
        def work(done):
            d = self._load_symbols()
            d.addCallbacks(
                lambda _msg: done({name: light_symbol.symbolId
                                   for name, light_symbol in self._symbols.items()}),
                lambda f: done(error=f))
        return self._run(work)

    def resolve_symbol(self, candidates: list[str]) -> str:
        """First candidate name this broker actually offers (case-insensitive).

        Brokers name the same instrument differently (GER40 / DE40 / GER40.cash),
        so callers pass a candidate list rather than one hardcoded ticker."""
        names = list(self.list_symbols().keys())
        available = set(names)
        for candidate in candidates:
            if candidate.upper() in available:
                return candidate
        raise RuntimeError(f"none of {candidates} found; broker symbols e.g. "
                           f"{sorted(names)[:SYMBOL_SAMPLE_LIMIT]}")

    def get_symbol_details(self, symbol: str) -> dict:
        """Contract metadata needed to size orders and round prices."""
        def work(done):
            d = self._load_symbols()
            d.addCallback(lambda _msg: self._full_symbol(symbol))

            def fin(full_symbol):
                done(dict(
                    symbol_id=full_symbol.symbolId,
                    name=symbol.upper(),
                    digits=getattr(full_symbol, "digits", DEFAULT_PRICE_DIGITS),
                    lot_size=full_symbol.lotSize,
                    min_volume=full_symbol.minVolume,
                    max_volume=full_symbol.maxVolume,
                    step_volume=full_symbol.stepVolume,
                ))
            d.addCallbacks(fin, lambda f: done(error=f))
        return self._run(work)

    # ---------- market data ----------

    def get_trendbars(self, symbol: str, period: str, days: int) -> pd.DataFrame:
        """OHLCV bars for `symbol` over the last `days`, as HUMAN prices.

        `period` is a TRENDBAR_PERIODS key ("M1"/"M5"/"M15"/"M30"/"H1"/"H4"/
        "D1"); anything else raises ValueError.

        Returns a DataFrame with columns open/high/low/close/volume on a
        tz-naive UTC DatetimeIndex, sorted ascending.

        Deliberately NEUTRAL: the legacy per-strategy adapters
        (`bot/ctrader*.py`) additionally applied strategy-specific point
        scaling (raw engine points via 10^digits) and localized the index to
        EET. This client does neither — human prices and UTC — and any such
        convention is the caller's business.
        """
        if period not in TRENDBAR_PERIODS:
            raise ValueError(f"unknown trendbar period {period!r}; "
                             f"expected one of {sorted(TRENDBAR_PERIODS)}")

        def work(done):
            d = self._load_symbols()

            def ask_bars(_msg):
                req = ProtoOAGetTrendbarsReq()
                req.ctidTraderAccountId = self.account_id
                req.symbolId = self.symbol_id(symbol)
                req.period = TRENDBAR_PERIODS[period]
                now = datetime.now(timezone.utc)
                req.fromTimestamp = int((now - timedelta(days=days)).timestamp() * MS_PER_SECOND)
                req.toTimestamp = int(now.timestamp() * MS_PER_SECOND)
                d_bars = self.client.send(req)
                d_bars.addCallback(self._check_response)
                return d_bars

            def fin(msg):
                rows = []
                for bar in msg.trendbar:
                    low = bar.low
                    rows.append(dict(
                        ts=bar.utcTimestampInMinutes * SECONDS_PER_MINUTE,
                        open=(low + bar.deltaOpen) / PRICE_SCALE,
                        high=(low + bar.deltaHigh) / PRICE_SCALE,
                        low=low / PRICE_SCALE,
                        close=(low + bar.deltaClose) / PRICE_SCALE,
                        volume=bar.volume,
                    ))
                bars = pd.DataFrame(rows, columns=["ts", "open", "high", "low",
                                                   "close", "volume"])
                index = pd.to_datetime(bars.pop("ts"), unit="s", utc=True).dt.tz_localize(None)
                bars.index = index
                done(bars.sort_index())
            d.addCallback(ask_bars)
            d.addCallbacks(fin, lambda f: done(error=f))
        return self._run(work)

    def get_m1(self, symbol: str, days: int) -> pd.DataFrame:
        """M1 bars, human prices, tz-naive UTC index."""
        return self.get_trendbars(symbol, "M1", days)

    def get_m15(self, symbol: str, days: int) -> pd.DataFrame:
        """M15 bars, human prices, tz-naive UTC index."""
        return self.get_trendbars(symbol, "M15", days)

    def get_d1(self, symbol: str, days: int) -> pd.DataFrame:
        """D1 bars, human prices, tz-naive UTC index."""
        return self.get_trendbars(symbol, "D1", days)

    # ---------- account state ----------

    def reconcile(self) -> dict:
        """Open positions and pending orders: {"positions": [...], "orders": [...]}.

        Field access is getattr-defensive because which sub-fields a broker
        populates on an order varies with the order type."""
        def work(done):
            req = ProtoOAReconcileReq()
            req.ctidTraderAccountId = self.account_id
            d = self.client.send(req)

            def fin(resp):
                msg = Protobuf.extract(resp)
                positions = []
                for broker_position in msg.position:
                    trade_data = broker_position.tradeData
                    positions.append(dict(
                        position_id=broker_position.positionId,
                        label=getattr(trade_data, "label", "") or "",
                        side=(SIDE_BUY if trade_data.tradeSide == ProtoOATradeSide.BUY
                              else SIDE_SELL),
                        volume=trade_data.volume,
                        symbol_id=trade_data.symbolId,
                        # the broker's own fill price / protection levels, not
                        # whatever we requested when the order was sent.
                        entry_price=getattr(broker_position, "price", None),
                        stop_loss=getattr(broker_position, "stopLoss", None),
                        take_profit=getattr(broker_position, "takeProfit", None),
                        opened_ts=getattr(trade_data, "openTimestamp", None),
                    ))
                orders = []
                for broker_order in msg.order:
                    trade_data = broker_order.tradeData
                    orders.append(dict(
                        order_id=broker_order.orderId,
                        label=getattr(trade_data, "label", "") or "",
                        side=(SIDE_BUY if trade_data.tradeSide == ProtoOATradeSide.BUY
                              else SIDE_SELL),
                        symbol_id=trade_data.symbolId,
                        order_type=getattr(broker_order, "orderType", None),
                        volume=getattr(trade_data, "volume", None),
                        limit_price=getattr(broker_order, "limitPrice", None),
                        stop_price=getattr(broker_order, "stopPrice", None),
                    ))
                done(dict(positions=positions, orders=orders))
            d.addCallbacks(fin, lambda f: done(error=f))
        return self._run(work)

    def get_open_positions(self) -> list[dict]:
        """Open positions only (the position half of `reconcile`)."""
        return self.reconcile()["positions"]

    def get_deals(self, days: int = DEAL_WINDOW_DAYS) -> list[dict]:
        """Closing deals over the last `days`, stitched from <=1-week windows.

        cTrader rejects a ProtoOADealListReq spanning more than DEAL_WINDOW_DAYS,
        so a longer lookback is split here. All windows run inside ONE session
        (the reactor is started once per call, and reconnecting immediately
        after a close is refused by the demo server)."""
        lookback_days = max(MIN_DEAL_LOOKBACK_DAYS, int(days))

        def work(done):
            @defer.inlineCallbacks
            def flow():
                now = datetime.now(timezone.utc)
                window_start = now - timedelta(days=lookback_days)
                deals = []
                while window_start < now:
                    window_end = min(window_start + timedelta(days=DEAL_WINDOW_DAYS), now)
                    chunk = yield self._deal_list_step(
                        int(window_start.timestamp() * MS_PER_SECOND),
                        int(window_end.timestamp() * MS_PER_SECOND))
                    deals.extend(chunk)
                    window_start = window_end
                return deals

            d = flow()
            d.addCallbacks(lambda deals: done(deals), lambda f: done(error=f))
        return self._run(work)

    def _deal_list_step(self, from_ms: int, to_ms: int,
                        max_rows: int = DEFAULT_DEAL_MAX_ROWS):
        """Closing deals in [from_ms, to_ms) (session-chained deferred).
        Read-only: places nothing."""
        req = ProtoOADealListReq()
        req.ctidTraderAccountId = self.account_id
        req.fromTimestamp = int(from_ms)
        req.toTimestamp = int(to_ms)
        req.maxRows = max_rows
        d = self.client.send(req)
        d.addCallback(self._check_response)
        d.addCallback(self._parse_deals)
        return d

    # ---------- trading ----------

    def place_market_order(self, symbol: str, side: str, *, volume_lots: float,
                           sl_price: float | None = None,
                           tp_price: float | None = None,
                           label: str = "", comment: str = "") -> dict:
        """MARKET order, volume in lots, optional absolute SL/TP (human prices).

        SL/TP are rounded to the symbol's own price precision before sending:
        cTrader rejects a price with more decimal digits than the symbol
        allows (found live 2026-08-06), and derived stop levels routinely come
        out of float math as e.g. 26081.200000000004."""
        return self._send_order(symbol, side, order_type=ProtoOAOrderType.MARKET,
                                price=None, volume_lots=volume_lots,
                                sl_price=sl_price, tp_price=tp_price,
                                label=label, comment=comment)

    def place_limit_order(self, symbol: str, side: str, *, price: float,
                          volume_lots: float,
                          sl_price: float | None = None,
                          tp_price: float | None = None,
                          label: str = "", comment: str = "") -> dict:
        """LIMIT order at `price` (human price), volume in lots, optional SL/TP.
        Same per-symbol price rounding contract as `place_market_order`."""
        return self._send_order(symbol, side, order_type=ProtoOAOrderType.LIMIT,
                                price=price, volume_lots=volume_lots,
                                sl_price=sl_price, tp_price=tp_price,
                                label=label, comment=comment)

    def _send_order(self, symbol: str, side: str, *, order_type,
                    price: float | None, volume_lots: float,
                    sl_price: float | None, tp_price: float | None,
                    label: str, comment: str) -> dict:
        """Shared body of place_market_order / place_limit_order: load symbols,
        fetch the contract metadata, size the volume, round the prices, send.
        `order_type` is a ProtoOAOrderType value (MARKET / LIMIT)."""
        def work(done):
            d = self._load_symbols()
            d.addCallback(lambda _msg: self._full_symbol(symbol))

            def send_order(full_symbol):
                digits = getattr(full_symbol, "digits", DEFAULT_PRICE_DIGITS)
                req = ProtoOANewOrderReq()
                req.ctidTraderAccountId = self.account_id
                req.symbolId = full_symbol.symbolId
                req.orderType = order_type
                req.tradeSide = self._side_enum(side)
                req.volume = self._volume_from_lots(volume_lots, full_symbol)
                if price is not None:
                    req.limitPrice = round(float(price), digits)
                # Only set what the caller asked for: an unset protection
                # field means "no SL/TP", which is a valid order.
                if sl_price is not None:
                    req.stopLoss = round(float(sl_price), digits)
                if tp_price is not None:
                    req.takeProfit = round(float(tp_price), digits)
                req.label = label
                req.comment = comment
                d_order = self.client.send(req)
                d_order.addCallback(self._check_response)
                return d_order

            def fin(msg):
                position_id, order_id = self._extract_ids(msg)
                done(dict(ok=True, position_id=position_id, order_id=order_id, raw=msg))
            d.addCallback(send_order)
            d.addCallbacks(fin, lambda f: done(error=f))
        return self._run(work)

    def close_position(self, position_id: int, volume: int) -> dict:
        """Close `volume` Open API volume units of an open position."""
        def work(done):
            req = ProtoOAClosePositionReq()
            req.ctidTraderAccountId = self.account_id
            req.positionId = position_id
            req.volume = volume
            d = self.client.send(req)
            d.addCallback(self._check_response)
            d.addCallbacks(lambda msg: done(dict(ok=True, raw=msg)),
                           lambda f: done(error=f))
        return self._run(work)

    def amend_position_sltp(self, position_id: int, *, sl_price: float,
                            tp_price: float | None = None,
                            digits: int = DEFAULT_AMEND_PRICE_DIGITS) -> dict:
        """Replace an open position's protection levels.

        ProtoOAAmendPositionSLTPReq REPLACES BOTH levels: a field left unset
        is REMOVED from the position, not "kept as is" -- so the position's
        CURRENT take profit must always be passed back in as `tp_price`, or
        moving the stop silently strips the TP. tp_price=None means the
        position genuinely has no TP.

        `digits`: the symbol's price precision (see `get_symbol_details`) --
        cTrader rejects a price with more decimals than the symbol allows."""
        def work(done):
            req = ProtoOAAmendPositionSLTPReq()
            req.ctidTraderAccountId = self.account_id
            req.positionId = position_id
            req.stopLoss = round(float(sl_price), digits)
            if tp_price:
                req.takeProfit = round(float(tp_price), digits)
            d = self.client.send(req)
            d.addCallback(self._check_response)
            d.addCallbacks(lambda msg: done(dict(ok=True, raw=msg)),
                           lambda f: done(error=f))
        return self._run(work)

    def cancel_order(self, order_id: int) -> dict:
        """Cancel a pending order."""
        def work(done):
            req = ProtoOACancelOrderReq()
            req.ctidTraderAccountId = self.account_id
            req.orderId = order_id
            d = self.client.send(req)
            d.addCallback(self._check_response)
            d.addCallbacks(lambda msg: done(dict(ok=True, raw=msg)),
                           lambda f: done(error=f))
        return self._run(work)
