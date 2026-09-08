"""Fetch 1-minute OHLCV bars from Alpaca Market Data API for the stock universe.

Free tier notes:
  - IEX feed (SIP requires a paid plan): covers ~2-3% of volume but minute
    bar SHAPE (highs/lows/timing) is adequate for entry/exit timing research;
    absolute volume numbers are not representative.
  - History depth: minute bars available back to ~2016.
  - Rate limit: 200 requests/min on the free tier -- the client below pages
    automatically and this script sleeps between symbols.

Credentials: put in the repo-root `.env` file (git-ignored):
    ALPACA_API_KEY=...
    ALPACA_SECRET_KEY=...

Output: data/raw/<TICKER>/<TICKER>_1m.parquet (parquet, not CSV -- minute
data for 10 years is ~1-2M rows/ticker; CSV would be 10x larger and slow).

Usage:
    python -m scripts.fetch_stock_minute --tickers AAPL,MSFT --start 2016-01-01
    python -m scripts.fetch_stock_minute            # whole universe, 2016+
"""
from __future__ import annotations

import argparse
import os
import time
from pathlib import Path

import pandas as pd

from scripts.fetch_stock_universe import UNIVERSE
from utils.data import DATA_RAW

ALPACA_DATA_URL = "https://data.alpaca.markets/v2/stocks/{symbol}/bars"
PAGE_LIMIT = 10_000


def _load_env() -> tuple[str, str]:
    """Read Alpaca credentials from the environment or the repo-root .env file."""
    key = os.environ.get("ALPACA_API_KEY")
    secret = os.environ.get("ALPACA_SECRET_KEY")
    if not (key and secret):
        env_path = Path(__file__).resolve().parent.parent / ".env"
        if env_path.exists():
            for line in env_path.read_text().splitlines():
                line = line.strip()
                if line.startswith("ALPACA_API_KEY="):
                    key = line.split("=", 1)[1].strip()
                elif line.startswith("ALPACA_SECRET_KEY="):
                    secret = line.split("=", 1)[1].strip()
    if not (key and secret):
        raise SystemExit(
            "Alpaca credentials not found. Add ALPACA_API_KEY and "
            "ALPACA_SECRET_KEY to .env in the repo root (git-ignored)."
        )
    return key, secret


def fetch_minute_bars(symbol: str, start: str, end: str | None,
                      key: str, secret: str) -> pd.DataFrame:
    import requests

    headers = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}
    params = {
        "timeframe": "1Min",
        "start": f"{start}T00:00:00Z",
        "limit": PAGE_LIMIT,
        "adjustment": "split",  # split-adjusted so gaps/levels match daily data
        "feed": "iex",
    }
    if end:
        params["end"] = f"{end}T23:59:59Z"

    frames = []
    page_token = None
    while True:
        if page_token:
            params["page_token"] = page_token
        resp = requests.get(ALPACA_DATA_URL.format(symbol=symbol),
                            headers=headers, params=params, timeout=30)
        resp.raise_for_status()
        payload = resp.json()
        bars = payload.get("bars") or []
        if bars:
            frames.append(pd.DataFrame(bars))
        page_token = payload.get("next_page_token")
        if not page_token:
            break
        time.sleep(0.35)  # stay under 200 req/min

    if not frames:
        raise RuntimeError("no bars returned")
    df = pd.concat(frames, ignore_index=True)
    df = df.rename(columns={"t": "ts", "o": "open", "h": "high", "l": "low",
                            "c": "close", "v": "volume"})
    df["ts"] = pd.to_datetime(df["ts"])
    df = df.set_index("ts").sort_index()
    return df[["open", "high", "low", "close", "volume"]].astype(float)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tickers", default=None,
                    help="comma-separated subset; default = whole universe")
    ap.add_argument("--start", default="2016-01-01")
    ap.add_argument("--end", default=None)
    ap.add_argument("--sleep", type=float, default=1.0, help="pause between symbols")
    args = ap.parse_args()

    key, secret = _load_env()
    tickers = args.tickers.split(",") if args.tickers else UNIVERSE

    ok, failed = [], []
    for ticker in tickers:
        ticker = ticker.strip().upper()
        out = DATA_RAW / ticker / f"{ticker}_1m.parquet"
        try:
            df = fetch_minute_bars(ticker, args.start, args.end, key, secret)
            out.parent.mkdir(parents=True, exist_ok=True)
            df.to_parquet(out)
            print(f"Saved: {out} ({len(df)} bars, "
                  f"{df.index.min().date()}..{df.index.max().date()})")
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

