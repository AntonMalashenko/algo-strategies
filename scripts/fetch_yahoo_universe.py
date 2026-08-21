"""Refresh daily OHLCV for the S011 multi-asset scan universe (equity indices,
FX majors/crosses, gold) from Yahoo Finance's chart JSON API.

Same approach as `fetch_spy_daily.py`: query `query2.finance.yahoo.com`
directly (not the `yfinance` package), which answers cleanly through this
project's various sandboxes/proxies where other hosts (histdata.com,
Dukascopy, stooq.com) get blocked or reset.

Why this replaces the old FX/XAUUSD source: those files (data/raw/<PAIR>/
<PAIR>d1.csv) were previously sourced from Dukascopy/histdata with a
different schema (`Date` column, prices scaled x1e5 for FX / x1000 for
XAUUSD, `tick_volume`) and stopped updating on 2022-03-04 (found during the
S011 portfolio research, 2026-08-18). Yahoo gives cleaner, longer, and
CURRENT history for the same instruments -- and using Yahoo for equities,
FX, and gold makes the whole S011 multi-asset universe one consistent data
source/schema. Old files are not silently discarded: see the timestamped
backup this script's first run produced under `/tmp/s011_backup_20260818/`
on the device (not committed to the repo -- ad-hoc, copy it somewhere
durable if you want to keep it).

Output schema unified with fetch_spy_daily.py: `date,open,high,low,close,
volume`, unscaled real prices. No more manual `scale=1e5` / `scale=1000` in
downstream loaders for these instruments.

Honest caveats:
- FX/gold from Yahoo have `volume` mostly 0 (no real FX volume reported) --
  same limitation the old tick-derived files didn't have, traded off for
  having current data at all.
- XAUUSD is proxied by `GC=F` (COMEX gold futures), not spot -- Yahoo's
  `XAUUSD=X` ticker returns 404. Futures vs spot differ by a small,
  time-varying basis; not corrected for here.
- Equity indices switch to Yahoo's own tickers (^GDAXI, ^FCHI, ^FTSE,
  ^STOXX50E, ^NDX, ^DJI, ^GSPC, ^RUT) -- same tickers the pre-existing
  DAX/CAC40/etc. files already used (verified: values continuous across the
  refresh), so this is a refresh of the same series, not a source switch.

Usage:
    python scripts/fetch_yahoo_universe.py
"""
from __future__ import annotations

import datetime
import os
import sys
import time
from pathlib import Path

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw"

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}
CHART_URL = "https://query2.finance.yahoo.com/v8/finance/chart/{ticker}"
MAX_RETRIES = 3
RETRY_SLEEP_SECONDS = 2.0
REQUEST_SLEEP_SECONDS = 0.3

# name -> (yahoo ticker, output subfolder under data/raw/, output filename)
SYMBOLS = {
    "DOW":      ("^DJI",      "DOW", "DOW.csv"),
    "NASDAQ":   ("^NDX",      "NASDAQ", "NASDAQ.csv"),
    "SP500idx": ("^GSPC",     "SP500", "SP500.csv"),
    "RUSSELL":  ("^RUT",      "RUSSELL", "RUSSELL.csv"),
    "DAX":      ("^GDAXI",    "DAX", "DAX.csv"),
    "CAC40":    ("^FCHI",     "CAC40", "CAC40.csv"),
    "FTSE100":  ("^FTSE",     "FTSE100", "FTSE100.csv"),
    "ESTOXX50": ("^STOXX50E", "ESTOXX50", "ESTOXX50.csv"),
    "XAUUSD":   ("GC=F",      "XAUUSD", "XAUUSDd1.csv"),
    "EURUSD":   ("EURUSD=X",  "EURUSD", "EURUSDd1.csv"),
    "GBPUSD":   ("GBPUSD=X",  "GBPUSD", "GBPUSDd1.csv"),
    "USDJPY":   ("USDJPY=X",  "USDJPY", "USDJPYd1.csv"),
    "USDCHF":   ("USDCHF=X",  "USDCHF", "USDCHFd1.csv"),
    "USDCAD":   ("USDCAD=X",  "USDCAD", "USDCADd1.csv"),
    "AUDUSD":   ("AUDUSD=X",  "AUDUSD", "AUDUSDd1.csv"),
    "AUDJPY":   ("AUDJPY=X",  "AUDJPY", "AUDJPYd1.csv"),
    "EURGBP":   ("EURGBP=X",  "EURGBP", "EURGBPd1.csv"),
    "EURJPY":   ("EURJPY=X",  "EURJPY", "EURJPYd1.csv"),
    "EURCHF":   ("EURCHF=X",  "EURCHF", "EURCHFd1.csv"),
    "GBPJPY":   ("GBPJPY=X",  "GBPJPY", "GBPJPYd1.csv"),
}


def fetch_one(ticker: str) -> pd.DataFrame:
    """Full available history for one Yahoo ticker, as a clean daily OHLCV frame."""
    last_exc = None
    for _ in range(MAX_RETRIES):
        try:
            resp = requests.get(
                CHART_URL.format(ticker=ticker),
                params={"period1": 0, "period2": 9999999999, "interval": "1d"},
                headers=UA, timeout=20,
            )
            result = resp.json()["chart"]["result"][0]
            break
        except Exception as exc:  # noqa: BLE001 -- retry on anything, surface the last one
            last_exc = exc
            time.sleep(RETRY_SLEEP_SECONDS)
    else:
        raise RuntimeError(f"fetch failed for {ticker!r} after {MAX_RETRIES} attempts") from last_exc

    ts = result["timestamp"]
    quote = result["indicators"]["quote"][0]
    dates = [datetime.datetime.utcfromtimestamp(t).date().isoformat() for t in ts]
    df = pd.DataFrame({
        "date": dates, "open": quote["open"], "high": quote["high"],
        "low": quote["low"], "close": quote["close"], "volume": quote.get("volume"),
    })
    df = df.dropna(subset=["open", "high", "low", "close"]).drop_duplicates(subset=["date"], keep="last")
    df["volume"] = df["volume"].fillna(0).astype(int)
    return df.reset_index(drop=True)


def main() -> int:
    summary = []
    for name, (ticker, subdir, fname) in SYMBOLS.items():
        try:
            df = fetch_one(ticker)
        except Exception as exc:  # noqa: BLE001
            print(f"FAILED {name} ({ticker}): {exc}", file=sys.stderr)
            continue
        out_dir = RAW / subdir
        os.makedirs(out_dir, exist_ok=True)
        df.to_csv(out_dir / fname, index=False)
        summary.append((name, ticker, len(df), df["date"].iloc[0], df["date"].iloc[-1]))
        time.sleep(REQUEST_SLEEP_SECONDS)

    print(f"{'name':10s} {'ticker':12s} {'rows':>6s}  {'first':10s} {'last':10s}")
    for name, ticker, n, first, last in summary:
        print(f"{name:10s} {ticker:12s} {n:6d}  {first:10s} {last:10s}")
    return 0 if len(summary) == len(SYMBOLS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
