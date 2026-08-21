"""cTrader adapter extension for S011 (RSI(2)-portfolio) — adds D1 bars for
MANY instruments in one session, plus MARKET entry/exit sized from a target
dollar book, on top of the shared `CTraderAdapter` (bot/ctrader.py).

Reuses everything from bot.ctrader; only the S011-specific pieces (batched
multi-symbol daily bars, batched multi-symbol contract metadata, a
portfolio-shaped `run_live_cycle_multi`) are added here — same "subclass,
don't fork" pattern bot/ctrader_s007.py already established for S007.

Why S011 needs its OWN single-session flow (can't reuse S007's
`run_live_cycle`, which fetches exactly one symbol per cycle): S011 trades
up to 13 instruments from ONE account in ONE daily cycle, and
`CTraderAdapter._run()` drives the whole thing through one
`reactor.run()` — a Twisted reactor can only be run once per OS process
(see webapp/runner.py's module docstring), so fetching each instrument's
D1 bars via 13 separate top-level calls (13 separate `_run()`s) is not an
option the way it would be for 13 independent single-symbol cycles in 13
separate subprocesses. Everything below is chained inside ONE
`defer.inlineCallbacks` flow, same technique `run_live_cycle`/
`sync_snapshot` in ctrader_s007.py already use for their own multi-step
single-session needs.

NOTE (as in bot/ctrader.py / ctrader_s007.py): field/enum names follow the
Open API spec; verify order/volume details against the cTrader UI on the
first live (`--broker dry`) cycle before ever running `--broker execute`.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd

from bot.ctrader import CTraderAdapter, HAVE_SDK, Protobuf  # reuse shared adapter

if HAVE_SDK:
    from twisted.internet import defer
    from ctrader_open_api.messages.OpenApiMessages_pb2 import (
        ProtoOAGetTrendbarsReq, ProtoOANewOrderReq, ProtoOAClosePositionReq,
        ProtoOAReconcileReq, ProtoOASymbolByIdReq,
    )
    from ctrader_open_api.messages.OpenApiModelMessages_pb2 import (
        ProtoOAOrderType, ProtoOATradeSide, ProtoOATrendbarPeriod,
    )

PRICE_SCALE = 100000.0   # Open API trendbar/price integers = human_price * 1e5


class CTraderS011(CTraderAdapter):
    def __init__(self, creds: dict | None = None, require_account: bool = True):
        """Same construction contract as CTraderS007 -- creds=None reads
        .env/accounts.yml single-account resolution; a dict is the explicit
        multi-account shape (client_id, client_secret, access_token,
        account_id, host?) the DB-driven runner passes in."""
        if not HAVE_SDK:
            raise RuntimeError("pip install ctrader-open-api first")
        if creds is None:
            super().__init__(require_account=require_account)
            return
        from ctrader_open_api import Client, TcpProtocol, EndPoints
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

    # ---------- single-session step helpers (assume connected+authed) -------

    def _resolve_symbols_step(self, candidates_by_asset: dict[str, tuple[str, ...]]) -> dict:
        """First matching broker symbol name per asset, from each asset's own
        candidate-name tuple (mirrors CTraderS007.resolve_symbol, but for many
        assets at once against the ALREADY-loaded self._symbols, no extra
        round trip). Assets with no matching broker symbol are simply absent
        from the returned dict -- the caller decides whether that is fatal
        (see run_live_cycle_multi, which logs and skips a missing asset
        rather than failing the whole cycle for the other 11-12)."""
        up = {n.upper() for n in self._symbols.keys()}
        resolved = {}
        for asset, candidates in candidates_by_asset.items():
            for c in candidates:
                if c.upper() in up:
                    resolved[asset] = c
                    break
        return resolved

    def _get_daily_step(self, symbol: str, days: int):
        """D1 trendbars for ONE symbol, human index/price points (same
        PRICE_SCALE convention as CTraderS007._get_m1_step, just period=D1).
        Chained (not top-level `_run`) so many of these can run back to back
        inside one session -- see run_live_cycle_multi."""
        req = ProtoOAGetTrendbarsReq()
        req.ctidTraderAccountId = self.account
        req.symbolId = self.symbol_id(symbol)
        req.period = ProtoOATrendbarPeriod.D1
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
            if df.empty:
                return df
            # D1 bars: cTrader's own daily-bar close convention (broker/server
            # time, NOT necessarily each instrument's own exchange midnight --
            # see bot/s011_paper.py's module docstring for the "one fixed
            # cutover for a mixed-session universe" tradeoff this accepts).
            idx = pd.to_datetime(df.pop("ts"), unit="s", utc=True).dt.tz_localize(None)
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
                    symbol_id=td.symbolId,
                ))
            return out
        d.addCallback(fin)
        return d

    def _full_symbols_step(self, symbol_ids: list[int]):
        """Full ProtoOASymbol (lotSize/digits/min-max-stepVolume) for MANY
        symbols in ONE request (symbolId is a repeated field on
        ProtoOASymbolByIdReq -- same trick CTraderS007._lot_sizes_step uses,
        generalised here to keep the whole ProtoOASymbol, not just lotSize,
        since S011 also needs `digits` for price rounding and
        min/max/stepVolume for order sizing across up to 13 instruments)."""
        if not symbol_ids:
            return defer.succeed({})
        req = ProtoOASymbolByIdReq()
        req.ctidTraderAccountId = self.account
        for sid in symbol_ids:
            req.symbolId.append(int(sid))
        d = self.client.send(req)
        d.addCallback(self._check_response)
        d.addCallback(lambda m: {s.symbolId: s for s in m.symbol})
        return d

    @staticmethod
    def _check_response(resp):
        """Same silent-rejection trap CTraderS007._check_response guards
        against (see that method's docstring) -- reused verbatim rather than
        re-derived, since it is broker-protocol behaviour, not S007-specific."""
        msg = Protobuf.extract(resp)
        name = type(msg).__name__
        if name in ("ProtoOAErrorRes", "ProtoOAOrderErrorEvent"):
            raise RuntimeError(f"{msg.errorCode}: {getattr(msg, 'description', '')}")
        if hasattr(msg, "payloadType") and hasattr(msg, "payload") and not hasattr(msg, "errorCode"):
            raise RuntimeError(
                f"unrecognized broker response (payloadType={msg.payloadType}) -- "
                f"treating as a failure, not assuming success")
        return msg

    @staticmethod
    def _volume_from_notional(notional: float, price: float, full_symbol) -> int:
        """Target dollar notional (in the SYMBOL's own quote currency -- see
        run_live_cycle_multi's docstring for the currency-conversion caveat
        this does NOT yet solve) -> Open API volume units, clamped to
        [minVolume, maxVolume] and rounded to a stepVolume multiple. Mirrors
        CTraderS007._volume_from_lots's clamping, but starting from a dollar
        target and a price rather than an already-decided lot count, since
        S011 sizes positions as `cap_pct * equity` in cash, not in lots."""
        if price <= 0:
            return 0
        volume_lots = notional / (price * full_symbol.lotSize)
        step = full_symbol.stepVolume or 1
        raw = volume_lots * 100 * full_symbol.lotSize
        raw = max(full_symbol.minVolume, min(full_symbol.maxVolume, raw))
        return int(round(raw / step) * step)

    def _place_market_step(self, symbol: str, side: str, notional: float, price: float,
                           label: str, full_symbol):
        req = ProtoOANewOrderReq()
        req.ctidTraderAccountId = self.account
        sym = self._symbols[symbol.upper()]
        req.symbolId = sym.symbolId
        req.orderType = ProtoOAOrderType.MARKET
        req.tradeSide = ProtoOATradeSide.BUY if side == "buy" else ProtoOATradeSide.SELL
        req.volume = self._volume_from_notional(notional, price, full_symbol)
        req.label = label
        req.comment = "S011"
        # No stopLoss/takeProfit -- RSI(2) is deliberately unprotected at the
        # single-position level, same as strategies/rsi2.py and
        # strategies/rsi2_portfolio.py (see both modules' docstrings); this
        # mirrors the backtest, it is not an oversight.
        d = self.client.send(req)
        d.addCallback(self._check_response)
        return d

    def _close_position_step(self, position_id: int, volume: int):
        req = ProtoOAClosePositionReq()
        req.ctidTraderAccountId = self.account
        req.positionId = position_id
        req.volume = volume
        d = self.client.send(req)
        d.addCallback(self._check_response)
        return d

    # ---------- one session, whole portfolio cycle ----------

    def run_live_cycle_multi(self, candidates_by_asset: dict[str, tuple[str, ...]],
                             history_days: int, decide):
        """One connect/auth/work/disconnect session for a full S011 cycle
        across every resolved asset -- see the module docstring for why this
        must be a single session/single `reactor.run()`, unlike S007's
        one-symbol-per-cycle `run_live_cycle`.

        Resolves every asset's broker symbol, fetches D1 bars + open
        positions + full contract metadata for the resolved set, then calls
          decide(daily_bars: dict[asset, DataFrame], positions: list[dict],
                 balance: float, symbol_meta: dict[asset, ProtoOASymbol],
                 last_price: dict[asset, float],
                 resolved: dict[asset, symbol_name]) -> list[action]
        (pure Python, no I/O) where each action is
          {"kind": "open", asset, symbol, side, notional, label} or
          {"kind": "close", asset, symbol, position_id, volume, label}
        and executes the actions in order. Returns
          {"resolved", "unresolved", "daily_bars", "positions", "actions",
           "results", "balance"}.

        `unresolved` (assets in candidates_by_asset with no broker match) is
        always returned, never silently dropped -- a paper/off cycle should
        still surface "S011 wanted 13 assets, broker only matched 11" so the
        gap is visible before the demo/dry stage (see decisions-log.md's open
        item on verifying the S011 universe against this broker's actual
        symbol list).
        """
        def work(done):
            @defer.inlineCallbacks
            def flow():
                yield self._load_symbols()
                resolved = self._resolve_symbols_step(candidates_by_asset)
                unresolved = sorted(set(candidates_by_asset) - set(resolved))

                daily_bars: dict[str, pd.DataFrame] = {}
                for asset, symbol in resolved.items():
                    daily_bars[asset] = yield self._get_daily_step(symbol, history_days)

                balance = yield self._get_balance_step()
                positions = yield self._reconcile_step()

                symbol_ids = [self._symbols[s.upper()].symbolId for s in resolved.values()]
                by_id = yield self._full_symbols_step(symbol_ids)
                symbol_meta = {asset: by_id[self._symbols[symbol.upper()].symbolId]
                              for asset, symbol in resolved.items()
                              if self._symbols[symbol.upper()].symbolId in by_id}
                last_price = {asset: float(df["close"].iloc[-1])
                             for asset, df in daily_bars.items() if not df.empty}

                actions = decide(daily_bars, positions, balance, symbol_meta, last_price, resolved)

                results = []
                for a in actions:
                    try:
                        if a["kind"] == "open":
                            r = yield self._place_market_step(
                                a["symbol"], a["side"], a["notional"],
                                last_price[a["asset"]], a["label"],
                                symbol_meta[a["asset"]])
                        else:
                            r = yield self._close_position_step(a["position_id"], a["volume"])
                        results.append(dict(action=a, result=r, error=None))
                    except Exception as e:
                        results.append(dict(action=a, result=None, error=e))

                return dict(resolved=resolved, unresolved=unresolved, daily_bars=daily_bars,
                            positions=positions, actions=actions, results=results, balance=balance)

            d = flow()
            d.addCallbacks(lambda r: done(r), lambda f: done(error=f))
        return self._run(work)
