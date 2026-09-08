"""Fetch historical earnings-announcement dates for the stock universe.

Saves per ticker: ``data/raw/<TICKER>/<TICKER>_earnings.csv`` with a single
``date`` column (announcement timestamp, tz dropped, one row per event).

Used by backtest/run_gap_earnings_split.py to separate gap events that were
caused by an earnings report from "no-news" gaps.

Usage:
    python -m scripts.fetch_earnings_dates
"""
from __future__ import annotations

import argparse
import time

import pandas as pd
import yfinance as yf

from scripts.fetch_stock_universe import UNIVERSE
from utils.data import DATA_RAW


def fetch_one(ticker: str, limit: int = 100) -> pd.DataFrame:
    ed = yf.Ticker(ticker).get_earnings_dates(limit=limit)
    if ed is None or ed.empty:
        raise RuntimeError("no earnings dates returned")
    idx = ed.index.tz_localize(None) if ed.index.tz is not None else ed.index
    return pd.DataFrame({"date": idx.sort_values()})


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=100,
                    help="max announcements per ticker (Yahoo caps at 100)")
    ap.add_argument("--sleep", type=float, default=0.5)
    args = ap.parse_args()

    ok, failed = [], []
    for ticker in UNIVERSE:
        try:
            df = fetch_one(ticker, args.limit)
            out = DATA_RAW / ticker / f"{ticker}_earnings.csv"
            out.parent.mkdir(parents=True, exist_ok=True)
            df.to_csv(out, index=False)
            print(f"Saved: {out} ({len(df)} events, "
                  f"{df['date'].min().date()}..{df['date'].max().date()})")
            ok.append(ticker)
        except Exception as exc:  # noqa: BLE001 - report and continue with the rest
            print(f"FAILED {ticker}: {exc}")
            failed.append(ticker)
        time.sleep(args.sleep)

    print(f"\nDone: {len(ok)} fetched, {len(failed)} failed.")
    if failed:
        print("Failed tickers:", ", ".join(failed))


if __name__ == "__main__":
    main()



