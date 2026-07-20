"""Download G10 short-term interest rates from FRED (no API key needed).

Feeds S005 (FX carry): each currency's 3-month interbank rate is a carry
proxy. FRED's fredgraph.csv endpoint serves any public series as CSV without
authentication, so this needs nothing beyond `requests`.

Run from a machine with internet access (network is closed in the cloud
sandbox):

    python scripts/fetch_rates.py

Files land in data/raw/rates/<CCY>.csv with columns date,rate (rate in
percent per annum, monthly, not seasonally adjusted).

Series IDs verified on fred.stlouisfed.org, 2026-07-11 (OECD "3-Month or
90-Day Rates and Yields: Interbank Rates" series family, id pattern
IR3TIB01<CC>M156N). Euro area series covers EUR from 1999 onward (proxied by
national rates before that where FRED backfills it; not needed for our
2000+ study window).
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import requests

OUT = Path(__file__).resolve().parent.parent / "data" / "raw" / "rates"

FRED_CSV_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}"

# currency -> FRED series id (monthly, % p.a., 3-month interbank rate)
SERIES = {
    "USD": "IR3TIB01USM156N",
    "EUR": "IR3TIB01EZM156N",
    "JPY": "IR3TIB01JPM156N",
    "GBP": "IR3TIB01GBM156N",
    "CHF": "IR3TIB01CHM156N",
    "AUD": "IR3TIB01AUM156N",
    "NZD": "IR3TIB01NZM156N",
    "CAD": "IR3TIB01CAM156N",
    "NOK": "IR3TIB01NOM156N",
    "SEK": "IR3TIB01SEM156N",
}

START = "2000-01-01"


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    for ccy, series_id in SERIES.items():
        url = FRED_CSV_URL.format(series_id=series_id)
        try:
            df = pd.read_csv(url)
            df.columns = ["date", "rate"]
            df["date"] = pd.to_datetime(df["date"])
            df["rate"] = pd.to_numeric(df["rate"], errors="coerce")
            df = df.dropna(subset=["rate"])
            df = df[df["date"] >= START]
            out = OUT / f"{ccy}.csv"
            df.to_csv(out, index=False)
            print(f"OK  {ccy:4} {series_id:18} {len(df):4} months  "
                  f"{df['date'].min().date()}..{df['date'].max().date()}  -> {out.name}")
        except Exception as e:
            print(f"FAIL {ccy:4} {series_id:18} {e}")


if __name__ == "__main__":
    main()
