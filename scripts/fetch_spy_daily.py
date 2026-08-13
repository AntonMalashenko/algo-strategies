"""Download SPY daily OHLCV history for S011 (Larry Connors EOD mean-reversion
setups: RSI(2), ConnorsRSI, Double Seven, Multiple Days Up/Down, R3, RSI4).

Source: Yahoo Finance's chart JSON API, queried directly (not via the
`yfinance` package, which -- as of this writing -- gets an SSL connection
reset through this sandbox's egress proxy on its default `query1` host).
`query2.finance.yahoo.com` answers cleanly with HTTP 200 through the same
proxy, so that's the host used here. Stooq (`stooq.com`) is unreachable from
the cloud sandbox outright (TLS connection reset at the proxy) -- ruled out
for that reason, not a data-quality one.

No API key needed. `period1=0` asks for "since inception"; Yahoo clips it to
the actual first trade date itself (SPY: 1993-01-29), so this always returns
the maximum available history without needing to know or guess the start date.

Run:
    python scripts/fetch_spy_daily.py

Output: data/raw/SPY/SPYd1.csv, columns date,open,high,low,close,volume --
the layout utils/data.py's load_csv()/_normalize() expect, matching the
`<INSTRUMENT>/<INSTRUMENT><timeframe>.csv` convention already used for
EURUSDd1.csv etc.

Honest caveat: SPY is a single instrument / single data source. Yahoo's
"regular" OHLC (not adjusted-close-back-adjusted) is used deliberately --
Connors' setups trade the *unadjusted* daily range (RSI, SMA, the 7-day
high/low), and back-adjusting for dividends would quietly shift historical
highs/lows. SPY's dividend yield is low (~1.3-1.9%/yr) so the distortion
from NOT adjusting is small, but it is not zero over 30+ years; flagged here
rather than silently assumed away.
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path

import pandas as pd
import requests

OUT_DIR = Path(__file__).resolve().parent.parent / "data" / "raw" / "SPY"
OUT_FILE = OUT_DIR / "SPYd1.csv"

CHART_URL = "https://query2.finance.yahoo.com/v8/finance/chart/{symbol}"
SYMBOL = "SPY"
# period1=0 -> Yahoo clips to the real first trade date; period2 far in the
# future -> clips to "now". Both avoid hardcoding a start/end date that goes
# stale.
PARAMS = {"period1": 0, "period2": 9_999_999_999, "interval": "1d",
          "events": "history", "includeAdjustedClose": "true"}
# A browser UA avoids Yahoo's bot-detection 429 seen with curl's default UA.
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                         "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"}


def fetch_spy_daily() -> pd.DataFrame:
    resp = requests.get(CHART_URL.format(symbol=SYMBOL), params=PARAMS,
                         headers=HEADERS, timeout=30)
    resp.raise_for_status()
    payload = resp.json()
    result = payload["chart"]["result"]
    if not result:
        raise RuntimeError(f"Yahoo returned no chart result for {SYMBOL}: {payload}")
    result = result[0]

    ts = result["timestamp"]
    quote = result["indicators"]["quote"][0]
    df = pd.DataFrame({
        "date": pd.to_datetime(ts, unit="s", utc=True).tz_convert("America/New_York").tz_localize(None).normalize(),
        "open": quote["open"],
        "high": quote["high"],
        "low": quote["low"],
        "close": quote["close"],
        "volume": quote["volume"],
    })
    # A handful of the earliest/most-recent bars can come back with a null
    # OHLC set (a half-day or a still-forming current bar); drop, don't
    # fabricate.
    df = df.dropna(subset=["open", "high", "low", "close"])
    df = df.drop_duplicates(subset="date").sort_values("date").reset_index(drop=True)
    return df


def main():
    df = fetch_spy_daily()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    df.to_csv(OUT_FILE, index=False)
    print(f"OK  {SYMBOL:6} {len(df):5} days  "
          f"{df['date'].min().date()}..{df['date'].max().date()}  -> {OUT_FILE}")
    print(f"Fetched at {dt.datetime.now(dt.timezone.utc).isoformat()}")


if __name__ == "__main__":
    main()
