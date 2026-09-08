"""Fetch daily OHLCV for a fixed universe of liquid US stocks via yfinance.

Saves each ticker under ``data/raw/<TICKER>/<TICKER>_1d.csv`` using the
standard cache layout from utils/data.load_yf.

KNOWN LIMITATION (document in any experiment using this data): the universe
is the CURRENT set of liquid large caps, so any backtest on it carries
survivorship bias. Acceptable for a cheap Gate 0 direction check; NOT
acceptable for a final validation, which needs point-in-time constituents.

Usage:
    python -m scripts.fetch_stock_universe
    python -m scripts.fetch_stock_universe --start 2010-01-01
"""
from __future__ import annotations

import argparse
import time

from utils.data import load_yf

# Liquid US large caps with long trading history, spread across sectors.
UNIVERSE = [
    # Tech / semis
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "AMD", "INTC",
    "QCOM", "AVGO", "ORCL", "CRM", "ADBE", "NFLX", "CSCO", "TXN", "MU", "IBM",
    # Financials
    "JPM", "BAC", "C", "WFC", "GS", "MS", "AXP", "V", "MA",
    # Energy
    "XOM", "CVX", "COP", "SLB", "OXY",
    # Healthcare
    "UNH", "JNJ", "PFE", "MRK", "ABBV", "LLY", "BMY", "AMGN", "GILD",
    # Consumer
    "WMT", "COST", "HD", "LOW", "TGT", "NKE", "MCD", "SBUX", "DIS", "KO",
    "PEP", "PG",
    # Industrials / telecom
    "CAT", "DE", "BA", "GE", "HON", "UPS", "FDX", "T", "VZ", "CMCSA",
]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2010-01-01")
    ap.add_argument("--sleep", type=float, default=0.5,
                    help="pause between downloads to stay polite with yfinance")
    args = ap.parse_args()

    ok, failed = [], []
    for ticker in UNIVERSE:
        try:
            load_yf(ticker, start=args.start, interval="1d")
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

