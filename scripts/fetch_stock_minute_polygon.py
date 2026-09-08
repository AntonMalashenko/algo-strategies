"""Fetch 1-minute OHLCV bars from Polygon.io (rebranded Massive) aggregates API.

Free tier constraints (verified 2026-08):
  - 5 API requests/minute -- the fetcher sleeps ~13s between requests and
    honors 429 responses with a backoff, so a full-universe run takes ~1.5h;
    run it in the background.
  - ~2 years of minute history.
  - Bars are split-adjusted (``adjusted=true``).

Credentials: ``POLYGON_API_KEY`` in the environment or the repo-root ``.env``.

Output: ``data/raw/<TICKER>/<TICKER>_1m.parquet``. Timestamps are converted
to US/Eastern (exchange time) and regular trading hours only are kept
(09:30-16:00) -- premarket noise is not needed for the current research and
tripling the file size for it is not worth it.

Usage:
    python -m scripts.fetch_stock_minute_polygon --tickers AAPL --start 2024-08-01 --end 2024-08-10
    python -m scripts.fetch_stock_minute_polygon            # whole universe, max free history
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import time
from pathlib import Path

import pandas as pd
import requests

from scripts.fetch_stock_universe import UNIVERSE
from utils.data import DATA_RAW

AGGS_URL = "https://api.polygon.io/v2/aggs/ticker/{ticker}/range/1/minute/{start}/{end}"
PAGE_LIMIT = 50_000
REQUEST_GAP_S = 13.0   # 5 req/min free-tier budget with headroom
MAX_RETRIES = 5


def _load_key() -> str:
    key = os.environ.get("POLYGON_API_KEY")
    if not key:
        env_path = Path(__file__).resolve().parent.parent / ".env"
        if env_path.exists():
            for line in env_path.read_text().splitlines():
                if line.strip().startswith("POLYGON_API_KEY="):
                    key = line.split("=", 1)[1].strip()
    if not key:
        raise SystemExit("POLYGON_API_KEY not found in environment or .env")
    return key


def _get(url: str, params: dict) -> dict:
    for attempt in range(MAX_RETRIES):
        resp = requests.get(url, params=params, timeout=30)
        if resp.status_code == 429:
            wait = 30 * (attempt + 1)
            print(f"  rate-limited, sleeping {wait}s...")
            time.sleep(wait)
            continue
        resp.raise_for_status()
        return resp.json()
    raise RuntimeError("giving up after repeated 429 responses")


def fetch_minute_bars(ticker: str, start: str, end: str, key: str) -> pd.DataFrame:
    frames = []
    url = AGGS_URL.format(ticker=ticker, start=start, end=end)
    params = {"adjusted": "true", "sort": "asc", "limit": PAGE_LIMIT, "apiKey": key}
    while True:
        payload = _get(url, params)
        results = payload.get("results") or []
        if results:
            frames.append(pd.DataFrame(results))
        next_url = payload.get("next_url")
        if not next_url:
            break
        url, params = next_url, {"apiKey": key}
        time.sleep(REQUEST_GAP_S)

    if not frames:
        raise RuntimeError("no bars returned")
    df = pd.concat(frames, ignore_index=True)
    df = df.rename(columns={"t": "ts", "o": "open", "h": "high", "l": "low",
                            "c": "close", "v": "volume"})
    idx = (pd.to_datetime(df["ts"], unit="ms", utc=True)
             .dt.tz_convert("America/New_York").dt.tz_localize(None))
    df = df.set_index(idx).sort_index()
    df = df.between_time("09:30", "16:00")  # regular trading hours only
    return df[["open", "high", "low", "close", "volume"]].astype(float)


def main() -> None:
    two_years_ago = (dt.date.today() - dt.timedelta(days=729)).isoformat()
    ap = argparse.ArgumentParser()
    ap.add_argument("--tickers", default=None,
                    help="comma-separated subset; default = whole universe")
    ap.add_argument("--start", default=two_years_ago)
    ap.add_argument("--end", default=dt.date.today().isoformat())
    ap.add_argument("--skip-existing", action="store_true",
                    help="skip tickers that already have a parquet file")
    args = ap.parse_args()

    key = _load_key()
    tickers = args.tickers.split(",") if args.tickers else UNIVERSE

    ok, failed = [], []
    for ticker in tickers:
        ticker = ticker.strip().upper()
        out = DATA_RAW / ticker / f"{ticker}_1m.parquet"
        if args.skip_existing and out.exists():
            print(f"Skip (exists): {out}")
            continue
        try:
            df = fetch_minute_bars(ticker, args.start, args.end, key)
            out.parent.mkdir(parents=True, exist_ok=True)
            df.to_parquet(out)
            print(f"Saved: {out} ({len(df)} bars, "
                  f"{df.index.min()}..{df.index.max()})", flush=True)
            ok.append(ticker)
        except Exception as exc:  # noqa: BLE001 - report and continue with the rest
            print(f"FAILED {ticker}: {exc}", flush=True)
            failed.append(ticker)
        time.sleep(REQUEST_GAP_S)

    print(f"\nDone: {len(ok)} fetched, {len(failed)} failed.")
    if failed:
        print("Failed tickers:", ", ".join(failed))


if __name__ == "__main__":
    main()

