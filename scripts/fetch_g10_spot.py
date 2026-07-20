"""Download daily G10-vs-USD spot FX from FRED (no API key needed), for S005.

First cut of this script used yfinance, but verification turned up two
problems: AUDUSD=X on Yahoo only starts 2006-05-16 (4 years later than our
rate data allows), and USDNOK/USDSEK had a genuine ~7-month hole in 2003
plus a ~3-week hole in Aug 2008 -- a known Yahoo FX history gap that lands
right inside a stress-test window. FRED's official H.10 noon buying rates
(same fredgraph.csv endpoint already used by fetch_rates.py) go back to
1971 for most of these and have neither problem, so this replaces the
yfinance version outright.

Run from a machine with internet access (network is closed in the cloud
sandbox):

    python scripts/fetch_g10_spot.py

Files land in data/raw/spot_g10/<PAIR>.csv, columns date,close (H.10 is a
single daily noon rate, not OHLC). Quote convention matches the pairs
already in the repo and fetch_rates.py's currency set: EUR, GBP, AUD, NZD
are quoted as base/USD; JPY, CHF, CAD, NOK, SEK are quoted as USD/base.
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

OUT = Path(__file__).resolve().parent.parent / "data" / "raw" / "spot_g10"

FRED_CSV_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}"

# pair name -> FRED H.10 series id (daily noon buying rate)
SERIES = {
    "EURUSD": "DEXUSEU",  # USD per EUR, starts 1999-01-04
    "GBPUSD": "DEXUSUK",  # USD per GBP, starts 1971-01-04
    "AUDUSD": "DEXUSAL",  # USD per AUD, starts 1971-01-04
    "NZDUSD": "DEXUSNZ",  # USD per NZD
    "USDJPY": "DEXJPUS",  # JPY per USD, starts 1971-01-04
    "USDCHF": "DEXSZUS",  # CHF per USD
    "USDCAD": "DEXCAUS",  # CAD per USD
    "USDNOK": "DEXNOUS",  # NOK per USD, starts 1971-01-04
    "USDSEK": "DEXSDUS",  # SEK per USD
}

START = "2000-01-01"


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    for pair, series_id in SERIES.items():
        url = FRED_CSV_URL.format(series_id=series_id)
        try:
            df = pd.read_csv(url)
            df.columns = ["date", "close"]
            df["date"] = pd.to_datetime(df["date"])
            df["close"] = pd.to_numeric(df["close"], errors="coerce")
            df = df.dropna(subset=["close"])
            df = df[df["date"] >= START]
            out = OUT / f"{pair}.csv"
            df.to_csv(out, index=False)
            print(f"OK  {pair:8} {series_id:10} {len(df):5} days  "
                  f"{df['date'].min().date()}..{df['date'].max().date()}  -> {out.name}")
        except Exception as e:
            print(f"FAIL {pair:8} {series_id:10} {e}")


if __name__ == "__main__":
    main()
