"""Paper-trading runner for the frozen S019 gap-fade strategy (strategies/gap_fade.py).

Runs AFTER the US close (or any later day) and reconstructs what the frozen
rule would have traded on the target date, using fresh yfinance daily data
for signals and Polygon minute bars for execution. Appends results to
``reports/paper/gap_fade_paper_log.csv`` (idempotent per date -- rerunning
the same date is a no-op).

This is deliberately an END-OF-DAY reconstruction, not a live intraday bot:
signal data (prior closes, SPY vol, liquidity) uses only pre-open
information, and execution replays real minute bars, so the log is an
honest record of the frozen rule with zero execution ambiguity. Live/broker
execution differences (slippage vs the bar price) are exactly what a later
live phase would measure against this log.

Prerequisites:
  - POLYGON_API_KEY in .env (minute bars).
  - Cached earnings dates (scripts/fetch_earnings_dates.py) -- refresh
    monthly, announcements more than ~1 quarter ahead are not needed.

Usage:
    python -m bot.gap_fade_paper                 # last completed trading day
    python -m bot.gap_fade_paper --date 2026-08-28
"""
from __future__ import annotations

import argparse
import datetime as dt
from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf

from scripts.fetch_stock_minute_polygon import _load_key, fetch_minute_bars
from scripts.fetch_stock_universe import UNIVERSE
from strategies.gap_fade import FROZEN_GAP_FADE, GapFadeConfig, replay_gap_fade_trade
from utils.data import DATA_RAW

ROOT = Path(__file__).resolve().parent.parent
PAPER_LOG = ROOT / "reports" / "paper" / "gap_fade_paper_log.csv"
LOOKBACK_DAYS = 70  # enough for 20d rolling windows plus holidays


def last_completed_trading_day() -> dt.date:
    day = dt.date.today() - dt.timedelta(days=1)
    while day.weekday() >= 5:  # Sat/Sun
        day -= dt.timedelta(days=1)
    return day


def fetch_daily(tickers: list[str], end: dt.date) -> pd.DataFrame:
    start = end - dt.timedelta(days=LOOKBACK_DAYS)
    df = yf.download(tickers, start=start.isoformat(),
                     end=(end + dt.timedelta(days=1)).isoformat(),
                     interval="1d", auto_adjust=False, progress=False,
                     group_by="ticker")
    return df


def earnings_blackout(ticker: str, date: dt.date) -> bool:
    """True if an announcement falls on the trade date or up to 3 days before.

    Slightly wider than the research tagging (announcement day + next trading
    day) to cover weekends conservatively -- staying out of an extra day is
    cheaper than fading an earnings move.
    """
    path = DATA_RAW / ticker / f"{ticker}_earnings.csv"
    if not path.exists():
        return True  # unknown label -> stay out (mirrors research exclusion)
    dates = pd.to_datetime(pd.read_csv(path)["date"]).dt.date
    return any(date - dt.timedelta(days=3) <= d <= date for d in dates)


def build_signals(date: dt.date, cfg: GapFadeConfig) -> list[dict]:
    daily = fetch_daily(UNIVERSE + ["SPY"], date)

    spy = daily["SPY"].dropna()
    if spy.empty or spy.index[-1].date() != date:
        raise SystemExit(f"No SPY data for {date} yet -- market closed or data lag.")
    spy_vol = (spy["Close"].pct_change().rolling(20).std() * np.sqrt(252) * 100).shift(1)
    vol_today = float(spy_vol.iloc[-1])
    if not vol_today < cfg.calm_vol_max_pct:
        print(f"Regime filter: SPY 20d vol {vol_today:.1f}% >= {cfg.calm_vol_max_pct}% -> no trading.")
        return []

    signals = []
    for ticker in UNIVERSE:
        try:
            bars = daily[ticker].dropna()
        except KeyError:
            continue
        if bars.empty or bars.index[-1].date() != date or len(bars) < 25:
            continue
        prev_close = float(bars["Close"].iloc[-2])
        today_open = float(bars["Open"].iloc[-1])
        gap_pct = (today_open / prev_close - 1.0) * 100.0
        if not (-cfg.gap_max_pct < gap_pct <= -cfg.gap_min_pct):
            continue
        dollar_vol = float((bars["Close"] * bars["Volume"]).rolling(20).median().iloc[-2])
        if dollar_vol < cfg.min_dollar_vol:
            continue
        if earnings_blackout(ticker, date):
            continue
        signals.append({"ticker": ticker, "gap_pct": gap_pct,
                        "spy_vol": vol_today, "dollar_vol": dollar_vol})

    signals.sort(key=lambda s: abs(s["gap_pct"]), reverse=True)
    return signals[:cfg.max_trades_per_day]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None, help="YYYY-MM-DD, default: last trading day")
    args = ap.parse_args()
    date = dt.date.fromisoformat(args.date) if args.date else last_completed_trading_day()
    cfg = FROZEN_GAP_FADE

    if PAPER_LOG.exists():
        log = pd.read_csv(PAPER_LOG, parse_dates=["date"])
        if (log["date"].dt.date == date).any():
            print(f"{date} already in {PAPER_LOG.relative_to(ROOT)} -- nothing to do.")
            return
    else:
        log = pd.DataFrame()

    print(f"Paper run for {date} (frozen config: entry +{cfg.entry_delay_min}min, "
          f"stop -{cfg.stop_pct}%, exit {cfg.exit_time}, max {cfg.max_trades_per_day}/day)")
    signals = build_signals(date, cfg)
    rows = []
    if not signals:
        print("No qualifying signals.")
        rows.append({"date": date, "ticker": "", "gap_pct": np.nan, "status": "no_signal"})
    else:
        key = _load_key()
        for sig in signals:
            bars = fetch_minute_bars(sig["ticker"], date.isoformat(), date.isoformat(), key)
            trade = replay_gap_fade_trade(bars, cfg)
            if trade is None:
                print(f"  {sig['ticker']}: unusable session, skipped")
                continue
            net = trade["gross_ret"] - cfg.cost_bps / 1e4
            rows.append({"date": date, "ticker": sig["ticker"],
                         "gap_pct": sig["gap_pct"], "status": "traded",
                         "entry_time": trade["entry_time"], "entry_price": trade["entry_price"],
                         "exit_time": trade["exit_time"], "exit_price": trade["exit_price"],
                         "exit_reason": trade["exit_reason"],
                         "gross_ret": trade["gross_ret"], "net_ret": net})
            print(f"  {sig['ticker']}: gap {sig['gap_pct']:+.2f}%, "
                  f"entry {trade['entry_price']:.2f} @ {trade['entry_time'].time()}, "
                  f"exit {trade['exit_price']:.2f} @ {trade['exit_time'].time()} "
                  f"({trade['exit_reason']}), net {net:+.2%}")

    PAPER_LOG.parent.mkdir(parents=True, exist_ok=True)
    pd.concat([log, pd.DataFrame(rows)], ignore_index=True).to_csv(PAPER_LOG, index=False)
    print(f"Logged to {PAPER_LOG.relative_to(ROOT)}")

    traded = pd.read_csv(PAPER_LOG)
    traded = traded[traded["status"] == "traded"]
    if len(traded):
        print(f"\nPaper totals: {len(traded)} trades, mean {traded['net_ret'].mean() * 1e4:+.1f} bps, "
              f"win {(traded['net_ret'] > 0).mean():.1%}, "
              f"stopped {(traded['exit_reason'] == 'stop').mean():.1%}")


if __name__ == "__main__":
    main()


