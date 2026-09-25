"""S011 (RSI(2) portfolio) broker layer — the shared cTrader client plus the
few conventions that are genuinely S011's own.

S011 is the FIRST strategy migrated onto `bot/clients/ctrader/client.py`
(`CTraderApiClient`). Everything generic — connect/auth, OAuth2 access-token
refresh, symbol resolution, batched D1 bars, balance, reconcile, contract
metadata, notional-sized MARKET orders, closes — now lives in that shared
client and is reused verbatim, so a fix there reaches every strategy that
follows. The other strategies still run on the legacy `bot/ctrader.py`
adapter and will be moved over one at a time.

What stays here, because it is strategy convention rather than broker
protocol:

  * the D1 SESSION-DATE relabelling (see D1_SESSION_DATE_ROLL) — the shared
    client deliberately returns neutral UTC-stamped bars;
  * the still-forming-bar filter and the "last CLOSED price" used to size
    orders;
  * the portfolio-shaped cycle `run_live_cycle_multi`.

Why the whole cycle is ONE session: S011 trades up to 13 instruments from one
account in one daily cycle, and a Twisted reactor can only be run once per OS
process. The shared client solves this for good by running the reactor once on
a background thread and serving every call from it, so the plain sequential
code below all happens inside a single connect/auth.

NOTE (as in bot/ctrader.py): field/enum names follow the Open API spec; verify
order/volume details against the cTrader UI on the first live (`--broker dry`)
cycle before ever running `--broker execute`.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd

from bot.clients.ctrader.client import CTraderApiClient

# cTrader D1 trendbars are stamped at the broker-day OPEN (this broker's local
# midnight -- 21:00 UTC in summer, 22:00 UTC in winter), so a bar stamped
# "D 21:00Z" is the D+1 session. We relabel each D1 bar to the UTC date its
# broker-day CLOSES on == the session date, which is what the Yahoo-sourced
# backtest (backtest/run_rsi2_portfolio.py) dates its bars by. Verified live
# 2026-09-09 against IC Markets (every D1 bar stamped 21:00Z).
D1_SESSION_DATE_ROLL = "D"   # pandas ceil() frequency used for that relabelling:
                             # roll a broker-day OPEN stamp up to the UTC
                             # midnight that same broker-day CLOSES on

# The bar period S011 trades off. Named so the string never appears inline.
S011_BAR_PERIOD = "D1"
# Order bookkeeping sent to the broker, visible in the cTrader UI.
ORDER_COMMENT = "S011"


class CTraderS011:
    """Composes `CTraderApiClient` — it does NOT subclass it.

    The shared client is a complete broker API; S011 adds a date convention
    and a cycle shape on top, which is composition, not specialisation. That
    also keeps the client free of any S011 assumption, so the next strategy
    migrating over inherits nothing strategy-specific.
    """

    def __init__(self, creds: dict, on_token_refreshed=None):
        """`creds`: the per-account dict the DB-driven runner builds
        (client_id, client_secret, access_token, refresh_token?,
        token_expires_at?, account_id, host?).

        `on_token_refreshed`: called with the renewed credential fields when
        the client rotates an expired access token, so the caller can persist
        them. Without it a refresh is forgotten at the end of the cycle -- see
        webapp/runner.py::_token_persister.
        """
        self.client = CTraderApiClient(creds, on_token_refreshed=on_token_refreshed)

    # ---------- S011's D1 date convention ----------

    @staticmethod
    def _session_dated_index(utc_open_ts) -> pd.DatetimeIndex:
        """cTrader D1 open timestamps -> naive session-date index.

        `utc_open_ts`: a tz-aware (UTC) pd.Series/Index of D1 bar OPEN times.
        Returns a tz-naive DatetimeIndex normalised to the UTC midnight the
        broker-day CLOSES on (== the trading session's calendar date). A bar
        already opening exactly at 00:00 UTC (a GMT-based broker) is left on
        its own date; any evening open (21:00/22:00 UTC) rolls to the next
        day. DST-safe: both 21:00Z and 22:00Z ceil to the same next midnight.

        See D1_SESSION_DATE_ROLL for why this relabelling exists at all.
        """
        opens_utc = pd.DatetimeIndex(pd.to_datetime(utc_open_ts, utc=True))
        return opens_utc.ceil(D1_SESSION_DATE_ROLL).tz_localize(None)

    @classmethod
    def _to_session_dates(cls, bars: pd.DataFrame) -> pd.DataFrame:
        """Relabel the shared client's UTC-stamped D1 bars to session dates.

        The raw timestamp names the PREVIOUS calendar day (and, over a
        weekend, up to two days back) relative to the session the bar actually
        covers. Relabelling makes every downstream date comparison -- the
        paper ledger's `date`, the up-to-date short-circuit, the stale-feed
        guard, a reconciliation against the Yahoo-dated backtest -- talk about
        the same day the backtest does. This is still ONE fixed cutover for a
        mixed-session universe, NOT each instrument's own exchange midnight
        (see bot/s011_paper.py's module docstring for that tradeoff); it
        changes only the index, never the OHLC.
        """
        if bars.empty:
            return bars
        relabelled = bars.copy()
        relabelled.index = cls._session_dated_index(bars.index)
        return relabelled.sort_index()

    @staticmethod
    def _drop_forming_bar(bars: pd.DataFrame) -> pd.DataFrame:
        """Drop a still-forming current-UTC-day bar, if the feed returned one
        (broker D1 quirk -- it can hand back a last row for TODAY before that
        day's bar has actually closed). Single source of truth for this
        filter: bot/s011_paper.py's decide() reuses it for its RSI signal
        instead of re-deriving the same date comparison, so the signal and the
        order-sizing price can never silently diverge on which bar counts as
        "current" again -- see `_last_closed_price`'s docstring for the
        incident this guards against.

        (D1 convention: the index this reads is SESSION-dated, not stamped at
        the broker-day open -- see `_session_dated_index` -- so a
        still-forming current session labels as TODAY's UTC date and is what
        gets dropped here. In practice this broker's trendbar request only
        returns already-closed bars, so this is defence-in-depth.)"""
        if bars.empty:
            return bars
        today_utc = datetime.now(timezone.utc).date()
        return bars[bars.index.date < today_utc] if bars.index[-1].date() >= today_utc else bars

    @classmethod
    def _last_closed_price(cls, bars: pd.DataFrame) -> float | None:
        """Latest CLOSED D1 bar's close -- never a still-forming current-UTC-
        day bar (operates on the session-dated, forming-bar-filtered series;
        see `_session_dated_index` and `_drop_forming_bar`).
        run_live_cycle_multi's `last_price` used to skip this guard even
        though it feeds directly into order sizing -- found live 2026-08-19:
        an incomplete bar's close priced a $1500-target CAC40 order at what
        was actually a ~$8500 position (~5.7x oversized), invisible in the log
        because only the (correctly filtered) `bars["close"]` used for
        rsi_close ever got logged, never this one."""
        closed = cls._drop_forming_bar(bars)
        if closed.empty:
            return None
        return float(closed["close"].iloc[-1])

    # ---------- one session, whole portfolio cycle ----------

    def fetch_session_dated_bars(self, candidates_by_asset: dict[str, tuple[str, ...]],
                                 history_days: int):
        """Session-dated D1 bars per asset, in ONE read-only session.

        Returns `(bars_by_asset, resolved, unresolved)`. Shared by the live
        cycle below and by scripts/s011_check_d1_alignment.py, so the
        alignment check can never drift from what the live bot actually sees.
        """
        with self.client as broker:
            resolved = broker.resolve_symbols(candidates_by_asset)
            unresolved = sorted(set(candidates_by_asset) - set(resolved))
            utc_bars = broker.get_trendbars_many(
                list(resolved.values()), S011_BAR_PERIOD, history_days)
            bars_by_asset = {asset: self._to_session_dates(utc_bars[symbol])
                             for asset, symbol in resolved.items()}
            return bars_by_asset, resolved, unresolved

    def run_live_cycle_multi(self, candidates_by_asset: dict[str, tuple[str, ...]],
                             history_days: int, decide):
        """One connect/auth/work/disconnect session for a full S011 cycle
        across every resolved asset.

        Resolves every asset's broker symbol, fetches D1 bars + open
        positions + full contract metadata for the resolved set, then calls
          decide(daily_bars: dict[asset, DataFrame], positions: list[dict],
                 balance: float, symbol_meta: dict[asset, dict],
                 last_price: dict[asset, float],
                 resolved: dict[asset, symbol_name]) -> list[action]
        (pure Python, no I/O) where each action is
          {"kind": "open", asset, symbol, side, notional, label} or
          {"kind": "close", asset, symbol, position_id, volume, label}
        and executes the actions in order. Returns
          {"resolved", "unresolved", "daily_bars", "positions", "actions",
           "results", "balance"}.

        `decide` runs INSIDE the session, so the target-book decision and the
        resulting orders happen against the same broker state that was just
        read -- no second connect/auth, and no window for the book to move in
        between.

        `unresolved` (assets in candidates_by_asset with no broker match) is
        always returned, never silently dropped -- a paper/off cycle should
        still surface "S011 wanted 13 assets, broker only matched 11" so the
        gap is visible before the demo/dry stage.

        An action the broker rejects is recorded in `results` with its
        exception and does NOT abort the rest: one closed market must not cost
        the other 11 instruments their cycle (see bot/s011_paper.py's
        ALGODEV-34 note for how the caller then reverts that asset's state).
        """
        with self.client as broker:
            resolved = broker.resolve_symbols(candidates_by_asset)
            unresolved = sorted(set(candidates_by_asset) - set(resolved))

            utc_bars = broker.get_trendbars_many(
                list(resolved.values()), S011_BAR_PERIOD, history_days)
            daily_bars = {asset: self._to_session_dates(utc_bars[symbol])
                          for asset, symbol in resolved.items()}

            balance = broker.get_balance()
            positions = broker.get_open_positions()

            details_by_symbol = broker.get_symbols_details(list(resolved.values()))
            symbol_meta = {asset: details_by_symbol[symbol.upper()]
                           for asset, symbol in resolved.items()
                           if symbol.upper() in details_by_symbol}
            last_price = {asset: price for asset, bars in daily_bars.items()
                          if (price := self._last_closed_price(bars)) is not None}

            actions = decide(daily_bars, positions, balance, symbol_meta,
                             last_price, resolved)

            results = []
            for action in actions:
                try:
                    if action["kind"] == "open":
                        outcome = broker.place_market_order(
                            action["symbol"], action["side"],
                            notional=action["notional"],
                            notional_price=last_price[action["asset"]],
                            label=action["label"], comment=ORDER_COMMENT)
                    else:
                        outcome = broker.close_position(action["position_id"],
                                                        action["volume"])
                    results.append(dict(action=action, result=outcome, error=None))
                except Exception as error:
                    results.append(dict(action=action, result=None, error=error))

            return dict(resolved=resolved, unresolved=unresolved, daily_bars=daily_bars,
                        positions=positions, actions=actions, results=results,
                        balance=balance)
