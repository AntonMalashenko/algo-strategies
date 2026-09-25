"""Standalone cTrader Open API client (Spotware OpenApiPy, protobuf over TLS).

Install on the trading machine:

    pip install ctrader-open-api

What this is
------------
`CTraderApiClient` is a clean, self-contained synchronous facade over the
async Spotware SDK. It exposes the broker operations the bot actually uses
(connectivity check, accounts, balance, symbols, trendbars, reconcile, deals,
market/limit orders, close, amend SL/TP, cancel) as plain Python methods with
human prices and plain dicts — no Twisted in the caller's code.

One session per process, many calls
-----------------------------------
A Twisted reactor can only be run ONCE per OS process. An earlier version of
this module ran `reactor.run()` inside every public method, which meant the
SECOND call in a process could never work — invisible only because nothing
imported it yet. Any real strategy needs many operations per cycle (S011
alone wants D1 bars for ~12 instruments plus balance, reconcile, contract
metadata and orders), so that shape was unusable.

Instead, the reactor runs once, on a daemon thread, for the lifetime of the
process. The session (connect -> application auth -> account auth) is opened
lazily on the first call and then reused, and each public method hands its
protobuf work to the reactor thread via `threads.blockingCallFromThread`,
blocking until the answer arrives. Callers therefore write ordinary
sequential Python:

    with CTraderApiClient(creds) as client:
        bars = {symbol: client.get_d1(symbol, days=40) for symbol in universe}
        balance = client.get_balance()
        client.place_market_order("GER40", "buy", volume_lots=0.1)

Every operation is split in two: a `_*_step()` that returns a Deferred and
assumes an open session, and a thin public wrapper that runs it. Composite
flows can therefore reuse the steps without paying for a second session.

Token refresh
-------------
Opening a session first makes sure the access token is still valid, renewing
it through `bot.clients.ctrader.auth` when it is close to expiry, and
reporting the new token via the `on_token_refreshed` callback so the caller
can persist it. This has to happen BEFORE the reactor starts: the refresh is
blocking HTTP, and a session that dies on an expired token cannot be retried
in the same process. See that module's docstring for the live outage this
prevents.

Accepted duplication
--------------------
The session plumbing below is a DELIBERATE duplicate of `bot/ctrader.py` /
`bot/ctrader_s007.py`, accepted by the maintainer: this module is the clean
interface the strategies migrate onto one at a time (S011 first), and it must
not depend on the legacy per-strategy adapter hierarchy while that migration
is pending.

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
from bot.clients.ctrader import auth

try:
    from twisted.internet import defer, threads
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

# How long a public call may wait for the broker before giving up. Without a
# cap, a silently dropped TCP connection blocks the calling thread forever and
# the scheduled cycle never ends; the scheduler would then stack up cycles.
CALL_TIMEOUT_S = 60
# How long to wait for connect + application auth + account auth. Larger than a
# single call: it covers a TLS handshake and two round trips.
CONNECT_TIMEOUT_S = 90

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


# The Twisted reactor is a per-process singleton that can be run exactly once
# and never restarted, so the thread running it is module state, not client
# state: several clients (e.g. two accounts) in one process share it.
_reactor_lock = threading.Lock()
_reactor_thread: threading.Thread | None = None


def _ensure_reactor_running() -> None:
    """Start the reactor on a daemon thread, once per process.

    `installSignalHandlers=False` is mandatory off the main thread. The thread
    is a daemon so a forgotten `close()` can never keep the process alive.
    """
    global _reactor_thread
    from twisted.internet import reactor

    with _reactor_lock:
        if _reactor_thread is not None and _reactor_thread.is_alive():
            return
        if reactor._startedBefore:
            # Twisted flags a reactor that has already run and finished; it
            # cannot be restarted (raises ReactorNotRestartable).
            raise RuntimeError(
                "the Twisted reactor has already been stopped in this process and "
                "cannot be restarted -- open the cTrader client once per process, "
                "or run the cycle in a fresh subprocess")
        _reactor_thread = threading.Thread(
            target=reactor.run, kwargs={"installSignalHandlers": False},
            name="ctrader-reactor", daemon=True)
        _reactor_thread.start()
        # Wait for the reactor to actually be spinning: callFromThread before
        # startRunning silently queues instead of executing, which would make
        # the first call look like a hang.
        started = threading.Event()
        reactor.callWhenRunning(started.set)
        if not started.wait(CONNECT_TIMEOUT_S):
            raise RuntimeError("Twisted reactor failed to start")


class CTraderApiClient(BaseClient):
    """Synchronous cTrader Open API client: one session per public call.

    Constructed from a single credentials dict (see `BaseClient`):
    {client_id, client_secret, access_token, account_id, host?,
     require_account?, refresh_token?, token_expires_at?}.

    `on_token_refreshed` is called with the refreshed credentials dict
    whenever the access token had to be renewed, so the caller can persist
    the new token (and the possibly-rotated refresh token). Not persisting it
    is not fatal, but wastes a refresh on every cycle.
    """

    def __init__(self, creds: dict, *, on_token_refreshed=None):
        super().__init__(creds)
        if not HAVE_SDK:
            raise RuntimeError("pip install ctrader-open-api first")
        # .get, not [] -- a wholly absent key is exactly the case the
        # "missing cTrader credentials: ..." message below exists to name.
        self.client_id = creds.get("client_id")
        self.client_secret = creds.get("client_secret")
        self.access_token = creds.get("access_token")
        self.account_id = int(creds.get("account_id") or 0)
        self.refresh_token = creds.get("refresh_token")
        self.token_expires_at = creds.get("token_expires_at")
        self.on_token_refreshed = on_token_refreshed
        host = creds.get("host") or EndPoints.PROTOBUF_DEMO_HOST
        # require_account=False is for the pre-account bootstrap: listing the
        # accounts a token is authorised for needs application auth only.
        self.require_account = creds.get("require_account", True)
        missing = []
        if not self.client_id:
            missing.append("client_id")
        if not self.client_secret:
            missing.append("client_secret")
        if not self.access_token:
            missing.append("access_token")
        if self.require_account and not self.account_id:
            missing.append("account_id")
        if missing:
            raise RuntimeError(f"missing cTrader credentials: {', '.join(missing)}")
        self.client = Client(host, EndPoints.PROTOBUF_PORT, TcpProtocol)
        self._symbols = None           # NAME -> ProtoOALightSymbol
        self._session_open = False
        self._disconnect_reason = None

    # ---------- session plumbing ----------

    def __enter__(self) -> "CTraderApiClient":
        return self.open()

    def __exit__(self, *_exc_info) -> None:
        self.close()

    def open(self) -> "CTraderApiClient":
        """Refresh the token if needed, then connect and authenticate.

        Idempotent: calling it again on an open session is a no-op, so the
        public methods can call it lazily without the caller having to.
        """
        if self._session_open:
            return self
        self._ensure_fresh_token()
        _ensure_reactor_running()
        self._blocking_call(self._connect_step, timeout_s=CONNECT_TIMEOUT_S)
        self._session_open = True
        return self

    def close(self) -> None:
        """Drop the broker session.

        The reactor itself is deliberately left running: it cannot be
        restarted, so stopping it would break every later client in this
        process. It is a daemon thread and dies with the process.
        """
        if not self._session_open:
            return
        self._session_open = False
        self._symbols = None
        try:
            self.client.stopService()
        except Exception:
            # Best effort: the session is being torn down anyway, and a
            # failure here must not mask the caller's real error.
            pass

    def _ensure_fresh_token(self) -> None:
        """Renew the access token when it is expired or nearly so.

        Runs before the reactor is touched -- see the module docstring. A
        missing/unknown expiry counts as "expired", so the first cycle after
        this feature ships refreshes once and records a real expiry.
        """
        bundle = auth.TokenBundle.from_credentials({
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "token_expires_at": self.token_expires_at,
        })
        if bundle is None or not bundle.needs_refresh():
            return
        if not self.refresh_token:
            # Nothing to refresh WITH. Not fatal: the current token may still
            # work (this is also the state of every account authorised before
            # refresh tokens were stored), so let the session attempt auth and
            # report the broker's own verdict.
            return
        refreshed = auth.refresh_access_token(
            self.client_id, self.client_secret, self.refresh_token)
        self.access_token = refreshed.access_token
        self.refresh_token = refreshed.refresh_token
        self.token_expires_at = refreshed.expires_at.isoformat()
        self.creds.update(refreshed.as_credentials())
        if self.on_token_refreshed is not None:
            self.on_token_refreshed(refreshed.as_credentials())

    def _connect_step(self):
        """Connect, application auth, and (unless bootstrapping) account auth.

        Returns a Deferred firing once the session is usable. The SDK is
        callback-driven, so the connected/disconnected callbacks are bridged
        onto one Deferred here.
        """
        ready = defer.Deferred()

        def settle(result=None, error=None):
            if ready.called:
                return
            if error is not None:
                ready.errback(error)
            else:
                ready.callback(result)

        def on_connected(_client):
            req = ProtoOAApplicationAuthReq()
            req.clientId = self.client_id
            req.clientSecret = self.client_secret
            d = self.client.send(req)
            if self.require_account:
                d.addCallback(lambda _resp: self._auth_account())
            d.addCallbacks(lambda _resp: settle(True), lambda f: settle(error=f))

        def on_disconnected(_client, reason):
            self._session_open = False
            self._disconnect_reason = reason
            settle(error=RuntimeError(f"cTrader disconnected: {reason}"))

        self.client.setConnectedCallback(on_connected)
        self.client.setDisconnectedCallback(on_disconnected)
        self.client.startService()
        return ready

    def _blocking_call(self, step, *args, timeout_s: int = CALL_TIMEOUT_S, **kwargs):
        """Run `step(*args, **kwargs)` on the reactor thread and block for it.

        `step` must return a Deferred. The timeout turns a silently dropped
        connection into a normal exception instead of an endless wait, which
        matters because these calls run inside a scheduled cycle.
        """
        from twisted.internet import reactor

        def guarded():
            d = defer.maybeDeferred(step, *args, **kwargs)
            # addTimeout must be attached on the reactor thread; it cancels the
            # Deferred and errbacks with TimeoutError.
            d.addTimeout(timeout_s, reactor)
            return d

        try:
            return threads.blockingCallFromThread(reactor, guarded)
        except Exception as error:
            raise RuntimeError(f"cTrader error: {error}") from error

    def _call(self, step, *args, **kwargs):
        """Public-method entry point: ensure a session, then run one step."""
        self.open()
        return self._blocking_call(step, *args, **kwargs)

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
            self._symbols = {light_symbol.symbolName.upper(): light_symbol
                             for light_symbol in msg.symbol}
            return self._symbols
        d.addCallback(store)
        return d

    def _ensure_symbols_step(self):
        """Symbol list, loaded at most once per session.

        The old one-session-per-call design had to re-download the whole
        symbol list on every single operation. With a persistent session it is
        fetched once and reused, which is what makes per-symbol calls in a
        loop (S011's 12 instruments) cheap.
        """
        if self._symbols is not None:
            return defer.succeed(self._symbols)
        return self._load_symbols()

    def _refresh_symbols_step(self):
        """Force a re-download of the symbol list (e.g. after a broker change)."""
        self._symbols = None
        return self._ensure_symbols_step()

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
        d = self._full_symbols_step([self.symbol_id(symbol)])
        d.addCallback(lambda by_id: by_id[self.symbol_id(symbol)])
        return d

    def _full_symbols_step(self, symbol_ids: list[int]):
        """Full ProtoOASymbol for MANY ids in ONE request -> {symbol_id: symbol}.

        `symbolId` is a repeated field on ProtoOASymbolByIdReq, so the whole
        set costs one round trip. An empty request is short-circuited rather
        than sent, since the broker rejects it."""
        if not symbol_ids:
            return defer.succeed({})
        req = ProtoOASymbolByIdReq()
        req.ctidTraderAccountId = self.account_id
        for symbol_id in symbol_ids:
            req.symbolId.append(int(symbol_id))
        d = self.client.send(req)
        d.addCallback(self._check_response)
        d.addCallback(lambda msg: {symbol.symbolId: symbol for symbol in msg.symbol})
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
        @defer.inlineCallbacks
        def step():
            yield self._ensure_symbols_step()
            balance = yield self._balance_step()
            if symbols is None:
                return dict(balance=balance, symbols_total=len(self._symbols))
            found = [name for name in symbols if name.upper() in self._symbols]
            missing = [name for name in symbols if name.upper() not in self._symbols]
            return dict(balance=balance, symbols_found=found, symbols_missing=missing)
        return self._call(step)

    def list_accounts(self) -> list[dict]:
        """Accounts the access token is authorised for.

        Application auth is enough, so this works before an account id is
        known -- construct the client with `require_account=False` in creds
        for that bootstrap case."""
        def step():
            req = ProtoOAGetAccountListByAccessTokenReq()
            req.accessToken = self.access_token
            d = self.client.send(req)
            d.addCallback(lambda resp: [
                dict(account_id=account.ctidTraderAccountId, is_live=account.isLive)
                for account in Protobuf.extract(resp).ctidTraderAccount])
            return d
        return self._call(step)

    def _balance_step(self):
        """Account balance in the deposit currency (session-chained deferred)."""
        req = ProtoOATraderReq()
        req.ctidTraderAccountId = self.account_id
        d = self.client.send(req)
        d.addCallback(lambda resp: Protobuf.extract(resp).trader.balance / BALANCE_SCALE)
        return d

    def get_balance(self) -> float:
        """Account balance in the deposit currency."""
        return self._call(self._balance_step)

    # ---------- symbols ----------

    def list_symbols(self, *, refresh: bool = False) -> dict[str, int]:
        """{broker symbol name: symbol id} for every symbol on the account.

        `refresh=True` re-downloads instead of reusing the session's cache."""
        def step():
            d = self._refresh_symbols_step() if refresh else self._ensure_symbols_step()
            d.addCallback(lambda symbols: {name: light_symbol.symbolId
                                           for name, light_symbol in symbols.items()})
            return d
        return self._call(step)

    def resolve_symbol(self, candidates: list[str]) -> str:
        """First candidate name this broker actually offers (case-insensitive).

        Brokers name the same instrument differently (GER40 / DE40 / GER40.cash),
        so callers pass a candidate list rather than one hardcoded ticker."""
        resolved = self.resolve_symbols({"symbol": tuple(candidates)})
        if "symbol" not in resolved:
            names = sorted(self.list_symbols())
            raise RuntimeError(f"none of {candidates} found; broker symbols e.g. "
                               f"{names[:SYMBOL_SAMPLE_LIMIT]}")
        return resolved["symbol"]

    def resolve_symbols(self, candidates_by_key: dict[str, tuple[str, ...]]) -> dict[str, str]:
        """Resolve MANY instruments at once against one symbol-list download.

        `candidates_by_key` maps a caller-chosen key (an asset name, a
        strategy's own instrument id) to that instrument's candidate broker
        names, and the result maps the same keys to the first name this broker
        actually offers. Keys with no match are simply ABSENT rather than
        raising, so one unavailable instrument cannot fail a whole portfolio
        cycle -- the caller decides what a missing instrument means.
        """
        available = {name.upper() for name in self.list_symbols()}
        resolved = {}
        for key, candidates in candidates_by_key.items():
            for candidate in candidates:
                if candidate.upper() in available:
                    resolved[key] = candidate
                    break
        return resolved

    def get_symbol_details(self, symbol: str) -> dict:
        """Contract metadata needed to size orders and round prices."""
        return self.get_symbols_details([symbol])[symbol.upper()]

    def get_symbols_details(self, symbols: list[str]) -> dict[str, dict]:
        """Contract metadata for MANY symbols in ONE request.

        `symbolId` is a repeated field on ProtoOASymbolByIdReq, so a portfolio
        strategy pays one round trip instead of one per instrument. Keyed by
        UPPERCASED symbol name to match `list_symbols`' own casing."""
        @defer.inlineCallbacks
        def step():
            yield self._ensure_symbols_step()
            by_id = yield self._full_symbols_step(
                [self.symbol_id(name) for name in symbols])
            details = {}
            for name in symbols:
                full_symbol = by_id.get(self.symbol_id(name))
                if full_symbol is not None:
                    details[name.upper()] = self._symbol_details(name, full_symbol)
            return details
        return self._call(step)

    @staticmethod
    def _symbol_details(symbol: str, full_symbol) -> dict:
        """ProtoOASymbol -> the plain dict this client exposes."""
        return dict(
            symbol_id=full_symbol.symbolId,
            name=symbol.upper(),
            digits=getattr(full_symbol, "digits", DEFAULT_PRICE_DIGITS),
            lot_size=full_symbol.lotSize,
            min_volume=full_symbol.minVolume,
            max_volume=full_symbol.maxVolume,
            step_volume=full_symbol.stepVolume,
        )

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
        return self._call(self._trendbars_step, symbol, period, days)

    def _trendbars_step(self, symbol: str, period: str, days: int):
        """Trendbars for one symbol (session-chained deferred)."""
        @defer.inlineCallbacks
        def flow():
            yield self._ensure_symbols_step()
            req = ProtoOAGetTrendbarsReq()
            req.ctidTraderAccountId = self.account_id
            req.symbolId = self.symbol_id(symbol)
            req.period = TRENDBAR_PERIODS[period]
            now = datetime.now(timezone.utc)
            req.fromTimestamp = int((now - timedelta(days=days)).timestamp() * MS_PER_SECOND)
            req.toTimestamp = int(now.timestamp() * MS_PER_SECOND)
            msg = yield self.client.send(req).addCallback(self._check_response)
            return self._bars_frame(msg)
        return flow()

    @staticmethod
    def _bars_frame(msg) -> pd.DataFrame:
        """ProtoOAGetTrendbarsRes -> OHLCV frame on a tz-naive UTC index."""
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
        bars.index = pd.to_datetime(bars.pop("ts"), unit="s", utc=True).dt.tz_localize(None)
        return bars.sort_index()

    def get_trendbars_many(self, symbols: list[str], period: str,
                           days: int) -> dict[str, pd.DataFrame]:
        """Bars for MANY symbols over ONE session, keyed by the name passed in.

        Sequential by design: the Open API answers one trendbar request at a
        time per symbol anyway, and a portfolio strategy cares about paying a
        single connect/auth, not about parallelism."""
        if period not in TRENDBAR_PERIODS:
            raise ValueError(f"unknown trendbar period {period!r}; "
                             f"expected one of {sorted(TRENDBAR_PERIODS)}")

        @defer.inlineCallbacks
        def step():
            yield self._ensure_symbols_step()
            bars_by_symbol = {}
            for symbol in symbols:
                bars_by_symbol[symbol] = yield self._trendbars_step(symbol, period, days)
            return bars_by_symbol
        return self._call(step, timeout_s=CALL_TIMEOUT_S * max(1, len(symbols)))

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
        return self._call(self._reconcile_step)

    def _reconcile_step(self):
        """Open positions + pending orders (session-chained deferred)."""
        req = ProtoOAReconcileReq()
        req.ctidTraderAccountId = self.account_id
        d = self.client.send(req)

        def parse(resp):
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
            return dict(positions=positions, orders=orders)
        d.addCallback(parse)
        return d

    def get_open_positions(self) -> list[dict]:
        """Open positions only (the position half of `reconcile`)."""
        return self.reconcile()["positions"]

    def get_deals(self, days: int = DEAL_WINDOW_DAYS) -> list[dict]:
        """Closing deals over the last `days`, stitched from <=1-week windows.

        cTrader rejects a ProtoOADealListReq spanning more than DEAL_WINDOW_DAYS,
        so a longer lookback is split here. All windows run inside the one
        session (the demo server refuses an immediate reconnect)."""
        lookback_days = max(MIN_DEAL_LOOKBACK_DAYS, int(days))

        @defer.inlineCallbacks
        def step():
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
        return self._call(step)

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

    def place_market_order(self, symbol: str, side: str, *,
                           volume_lots: float | None = None,
                           notional: float | None = None,
                           notional_price: float | None = None,
                           sl_price: float | None = None,
                           tp_price: float | None = None,
                           label: str = "", comment: str = "") -> dict:
        """MARKET order, optional absolute SL/TP (human prices).

        Size it either way, whichever the strategy thinks in:
          * `volume_lots` -- a lot count (S007/S021 size by risk in lots);
          * `notional` + `notional_price` -- a target cash exposure in the
            symbol's quote currency at that price (S011 sizes a portfolio by
            dollar weight, not lots).
        Exactly one of the two must be given.

        SL/TP are rounded to the symbol's own price precision before sending:
        cTrader rejects a price with more decimal digits than the symbol
        allows (found live 2026-08-06), and derived stop levels routinely come
        out of float math as e.g. 26081.200000000004."""
        return self._send_order(symbol, side, order_type=ProtoOAOrderType.MARKET,
                                price=None, volume_lots=volume_lots,
                                notional=notional, notional_price=notional_price,
                                sl_price=sl_price, tp_price=tp_price,
                                label=label, comment=comment)

    def place_limit_order(self, symbol: str, side: str, *, price: float,
                          volume_lots: float | None = None,
                          notional: float | None = None,
                          notional_price: float | None = None,
                          sl_price: float | None = None,
                          tp_price: float | None = None,
                          label: str = "", comment: str = "") -> dict:
        """LIMIT order at `price` (human price). Same sizing and per-symbol
        price rounding contract as `place_market_order`."""
        return self._send_order(symbol, side, order_type=ProtoOAOrderType.LIMIT,
                                price=price, volume_lots=volume_lots,
                                notional=notional, notional_price=notional_price,
                                sl_price=sl_price, tp_price=tp_price,
                                label=label, comment=comment)

    def place_stop_order(self, symbol: str, side: str, *, stop_price: float,
                         volume_lots: float | None = None,
                         notional: float | None = None,
                         notional_price: float | None = None,
                         sl_price: float | None = None,
                         tp_price: float | None = None,
                         label: str = "", comment: str = "") -> dict:
        """STOP order triggering at `stop_price` (human price).

        This is the resting-entry shape S021 uses (buy-stop above the range,
        sell-stop below it); same sizing and rounding contract as the others."""
        return self._send_order(symbol, side, order_type=ProtoOAOrderType.STOP,
                                price=None, stop_price=stop_price,
                                volume_lots=volume_lots, notional=notional,
                                notional_price=notional_price,
                                sl_price=sl_price, tp_price=tp_price,
                                label=label, comment=comment)

    def _order_volume(self, full_symbol, *, volume_lots: float | None,
                      notional: float | None, notional_price: float | None) -> int:
        """Pick the sizing mode the caller asked for and return API volume units."""
        if (volume_lots is None) == (notional is None):
            raise ValueError("pass exactly one of volume_lots= or notional=")
        if volume_lots is not None:
            return self._volume_from_lots(volume_lots, full_symbol)
        if not notional_price:
            raise ValueError("notional= sizing also needs notional_price=")
        return self._volume_from_notional(notional, notional_price, full_symbol)

    @staticmethod
    def _volume_from_notional(notional: float, price: float, full_symbol) -> int:
        """Target cash exposure at `price` -> Open API volume units.

        Same clamping/stepping contract as `_volume_from_lots`, but starting
        from money rather than an already-decided lot count. `price` must be a
        CLOSED bar's price or a live quote -- sizing off a still-forming bar
        oversized a live S011 order ~5.7x on 2026-08-19."""
        if price <= 0:
            return 0
        volume_lots = notional / (price * full_symbol.lotSize)
        return CTraderApiClient._volume_from_lots(volume_lots, full_symbol)

    def _send_order(self, symbol: str, side: str, *, order_type,
                    price: float | None, volume_lots: float | None,
                    notional: float | None, notional_price: float | None,
                    sl_price: float | None, tp_price: float | None,
                    label: str, comment: str,
                    stop_price: float | None = None) -> dict:
        """Shared body of the place_*_order methods: load symbols, fetch the
        contract metadata, size the volume, round the prices, send.
        `order_type` is a ProtoOAOrderType value (MARKET / LIMIT / STOP)."""
        @defer.inlineCallbacks
        def step():
            yield self._ensure_symbols_step()
            full_symbol = yield self._full_symbol(symbol)
            digits = getattr(full_symbol, "digits", DEFAULT_PRICE_DIGITS)
            req = ProtoOANewOrderReq()
            req.ctidTraderAccountId = self.account_id
            req.symbolId = full_symbol.symbolId
            req.orderType = order_type
            req.tradeSide = self._side_enum(side)
            req.volume = self._order_volume(full_symbol, volume_lots=volume_lots,
                                            notional=notional,
                                            notional_price=notional_price)
            if price is not None:
                req.limitPrice = round(float(price), digits)
            if stop_price is not None:
                req.stopPrice = round(float(stop_price), digits)
            # Only set what the caller asked for: an unset protection
            # field means "no SL/TP", which is a valid order.
            if sl_price is not None:
                req.stopLoss = round(float(sl_price), digits)
            if tp_price is not None:
                req.takeProfit = round(float(tp_price), digits)
            req.label = label
            req.comment = comment
            msg = yield self.client.send(req).addCallback(self._check_response)
            position_id, order_id = self._extract_ids(msg)
            return dict(ok=True, position_id=position_id, order_id=order_id, raw=msg)
        return self._call(step)

    def close_position(self, position_id: int, volume: int) -> dict:
        """Close `volume` Open API volume units of an open position."""
        return self._call(self._close_position_step, position_id, volume)

    def _close_position_step(self, position_id: int, volume: int):
        """Close a position (session-chained deferred)."""
        req = ProtoOAClosePositionReq()
        req.ctidTraderAccountId = self.account_id
        req.positionId = position_id
        req.volume = volume
        d = self.client.send(req)
        d.addCallback(self._check_response)
        d.addCallback(lambda msg: dict(ok=True, raw=msg))
        return d

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
        def step():
            req = ProtoOAAmendPositionSLTPReq()
            req.ctidTraderAccountId = self.account_id
            req.positionId = position_id
            req.stopLoss = round(float(sl_price), digits)
            if tp_price:
                req.takeProfit = round(float(tp_price), digits)
            d = self.client.send(req)
            d.addCallback(self._check_response)
            d.addCallback(lambda msg: dict(ok=True, raw=msg))
            return d
        return self._call(step)

    def cancel_order(self, order_id: int) -> dict:
        """Cancel a pending order."""
        def step():
            req = ProtoOACancelOrderReq()
            req.ctidTraderAccountId = self.account_id
            req.orderId = order_id
            d = self.client.send(req)
            d.addCallback(self._check_response)
            d.addCallback(lambda msg: dict(ok=True, raw=msg))
            return d
        return self._call(step)
