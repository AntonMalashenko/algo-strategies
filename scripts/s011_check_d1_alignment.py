"""Read-only check that S011's live cTrader D1 bars are dated on the SAME
calendar the Yahoo-sourced backtest uses.

Background: this broker stamps every D1 trendbar at the broker-day OPEN
(21:00 UTC summer / 22:00 UTC winter -- its own local midnight), so a raw
`utcTimestampInMinutes` names the day BEFORE the session the bar covers.
`CTraderS011._session_dated_index` relabels each bar to the UTC date its
broker-day CLOSES on, which is the session date `backtest/
run_rsi2_portfolio.py::load_universe()` dates its own bars by. This script
verifies that relabelling against the live feed: for every DEPLOY_UNIVERSE
asset it prints the last sessions of relabelled D1 alongside the RSI(2)
signal, and -- when the backtest's data is available locally -- the
backtest's own rows for the same dates.

Closes WILL differ (broker CFD vs Yahoo cash index/ETF, different feeds and
snapshot times); the DATES must not. Only date-set differences are counted
as mismatches.

Opens ONE cTrader session and sends only ProtoOASymbolsListReq /
ProtoOAGetTrendbarsReq -- no orders, no reconcile, no state or ledger write.

Run from the repo root:
    .venv/bin/python scripts/s011_check_d1_alignment.py
    .venv/bin/python scripts/s011_check_d1_alignment.py --sessions 30 --asset CAC40
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
from bot.ctrader_s011 import CTraderS011, HAVE_SDK                  # noqa: E402
from bot.s011_paper import (                                        # noqa: E402
    CTRADER_SYMBOL_CANDIDATES, DEPLOY_UNIVERSE, HISTORY_DAYS,
)
from strategies.rsi2 import BASELINE_RSI2, rsi2_signal              # noqa: E402

SESSIONS_SHOWN = 20   # rows printed per asset; the FETCH window stays
                      # HISTORY_DAYS so RSI(2)'s trend_sma(200) is warmed up
                      # and `rsi2_held` is the real signal, not a warmup NaN


class _D1AlignmentProbe(CTraderS011):
    """Read-only subclass: one session, D1 bars for many symbols, nothing
    else. Reuses the adapter's own `_load_symbols` / `_resolve_symbols_step`
    / `_get_daily_step` (and therefore `_session_dated_index`) so this script
    can never drift from what the live bot actually sees."""

    def fetch_session_dated_bars(self, candidates_by_asset: dict[str, tuple[str, ...]],
                                 days: int) -> tuple[dict[str, pd.DataFrame], dict[str, str], list[str]]:
        from twisted.internet import defer

        def work(done):
            @defer.inlineCallbacks
            def flow():
                yield self._load_symbols()
                resolved = self._resolve_symbols_step(candidates_by_asset)
                unresolved = sorted(set(candidates_by_asset) - set(resolved))
                bars_by_asset: dict[str, pd.DataFrame] = {}
                for asset, symbol in resolved.items():
                    bars_by_asset[asset] = yield self._get_daily_step(symbol, days)
                return bars_by_asset, resolved, unresolved

            d = flow()
            d.addCallbacks(lambda r: done(r), lambda f: done(error=f))
        return self._run(work)


def _load_backtest_universe() -> dict[str, pd.DataFrame]:
    """The backtest's own Yahoo/Bybit-sourced bars, or {} when the raw data
    (or the module) isn't available on this machine -- the broker side of the
    check still runs without it."""
    try:
        from backtest.run_rsi2_portfolio import load_universe
        return load_universe()
    except Exception as exc:
        print(f"[warn] backtest universe unavailable ({exc!r}) -- broker side only")
        return {}


def _report_asset(asset: str, symbol: str, broker_bars: pd.DataFrame,
                  backtest_bars: pd.DataFrame | None, sessions: int) -> int:
    """Print one asset's table and return its number of DATE mismatches."""
    closed = CTraderS011._drop_forming_bar(broker_bars)
    if closed.empty:
        print(f"\n=== {asset} ({symbol}): no closed D1 bars returned ===")
        return 0
    held = rsi2_signal(closed, BASELINE_RSI2)
    window = closed.tail(sessions)

    backtest_window = None
    if backtest_bars is not None and not backtest_bars.empty:
        backtest_window = backtest_bars.loc[
            (backtest_bars.index >= window.index[0]) & (backtest_bars.index <= window.index[-1])]
        if backtest_window.empty:
            backtest_window = None
        else:
            backtest_held = rsi2_signal(backtest_bars, BASELINE_RSI2)

    print(f"\n=== {asset} ({symbol}) -- last {len(window)} session-dated D1 bars ===")
    if backtest_window is None:
        print(f"{'session_date':>12} {'close':>12} {'rsi2_held':>10}")
        for ts, row in window.iterrows():
            print(f"{ts.date().isoformat():>12} {row['close']:>12.2f} {int(held.loc[ts]):>10}")
        return 0

    print(f"{'session_date':>12} {'close':>12} {'rsi2_held':>10} | "
          f"{'bt_date':>12} {'bt_close':>12} {'bt_held':>8}")
    for ts, row in window.iterrows():
        if ts in backtest_window.index:
            bt = (f"{ts.date().isoformat():>12} {backtest_window.loc[ts, 'close']:>12.2f} "
                  f"{int(backtest_held.loc[ts]):>8}")
        else:
            bt = f"{'--':>12} {'--':>12} {'--':>8}"
        print(f"{ts.date().isoformat():>12} {row['close']:>12.2f} "
              f"{int(held.loc[ts]):>10} | {bt}")

    broker_only = sorted(set(window.index) - set(backtest_window.index))
    backtest_only = sorted(set(backtest_window.index) - set(window.index))
    for ts in broker_only:
        print(f"  MISMATCH: {ts.date()} in broker D1 but not in the backtest")
    for ts in backtest_only:
        print(f"  MISMATCH: {ts.date()} in the backtest but not in broker D1")
    return len(broker_only) + len(backtest_only)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--sessions", type=int, default=SESSIONS_SHOWN,
                    help=f"sessions printed per asset (default {SESSIONS_SHOWN})")
    ap.add_argument("--history-days", type=int, default=HISTORY_DAYS,
                    help="D1 history fetched, for RSI(2)/SMA warmup")
    ap.add_argument("--asset", action="append", dest="assets",
                    help="limit to this DEPLOY_UNIVERSE asset (repeatable)")
    args = ap.parse_args()

    if not HAVE_SDK:
        sys.exit("ctrader-open-api is not installed -- cannot reach the broker")
    assets = tuple(args.assets) if args.assets else DEPLOY_UNIVERSE
    unknown = [a for a in assets if a not in CTRADER_SYMBOL_CANDIDATES]
    if unknown:
        sys.exit(f"unknown asset(s): {unknown} -- pick from {list(DEPLOY_UNIVERSE)}")
    candidates = {a: CTRADER_SYMBOL_CANDIDATES[a] for a in assets}

    probe = _D1AlignmentProbe()
    broker_bars, resolved, unresolved = probe.fetch_session_dated_bars(
        candidates, args.history_days)
    if unresolved:
        print(f"[warn] no broker symbol matched: {unresolved}")

    backtest_universe = _load_backtest_universe()
    mismatches = 0
    for asset in sorted(broker_bars):
        mismatches += _report_asset(asset, resolved[asset], broker_bars[asset],
                                    backtest_universe.get(asset), args.sessions)
    print(f"\n{len(broker_bars)} assets checked, {mismatches} date mismatches")


if __name__ == "__main__":
    main()
