"""Download crypto perpetual OHLCV history from Bybit v5 (public), for S008.

S008 studies a multi-timeframe + ML crypto strategy ported from the external
`tradingbot` engine. The research question is a *configuration search*: on which
instrument(s) and with which parameters the strategy pays off net of costs. This
script pulls the raw candles the backtest needs.

Bybit's kline endpoint is PUBLIC — no API key/secret required. We hit mainnet
(`api.bybit.com`) because testnet has almost no usable history. Only `requests`
and `pandas` are needed (no pybit dependency), so it runs from the `algo` venv.

Run from a machine with internet access (the cloud sandbox has no network):

    python scripts/fetch_crypto_bybit.py
    python scripts/fetch_crypto_bybit.py --symbols BTCUSDT ETHUSDT --days 1500

For every symbol × timeframe we fetch as far back as Bybit serves (younger coins
simply return less), then write ascending, de-duplicated candles to
`data/raw/crypto/<SYMBOL>/<tf>.csv` with columns:

    ts,datetime,open,high,low,close,volume

`ts` is the candle-open time in epoch-milliseconds (UTC); `datetime` is the same
instant as an ISO-8601 UTC string, kept for human readability. Keeping the
timestamp is mandatory here: the backtest aligns M15 entries with the H1/H4/D1
context and slices train/test windows by date, none of which is possible from
bare OHLCV rows.
"""
from __future__ import annotations

import argparse
import time
from pathlib import Path

import pandas as pd
import requests

BYBIT_KLINE_URL = "https://api.bybit.com/v5/market/kline"

# Liquid basket chosen for the S008 configuration search (2026-07-19).
DEFAULT_SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT"]

# Bybit interval code -> output filename stem.
TIMEFRAMES = {"15": "m15", "60": "h1", "240": "h4", "D": "d1"}

# Minutes per interval, to translate a day budget into a candle target.
_INTERVAL_MINUTES = {"15": 15, "60": 60, "240": 240, "D": 1440}

OUT_ROOT = Path(__file__).resolve().parent.parent / "data" / "raw" / "crypto"

PAGE_LIMIT = 1000          # Bybit hard cap per kline request
SLEEP_BETWEEN = 0.2        # polite pause between pages (rate limit)


def fetch_klines(symbol: str, interval: str, days: int,
                 category: str = "linear") -> pd.DataFrame:
    """Paginate Bybit klines backwards until `days` of history (or the listing
    start) is covered. Returns an ascending, de-duplicated OHLCV DataFrame."""
    minutes = _INTERVAL_MINUTES.get(interval, 15)
    target = int(days * 24 * 60 / minutes)

    rows: list[list] = []
    end_ts: int | None = None

    while len(rows) < target:
        params = {
            "category": category,
            "symbol": symbol,
            "interval": interval,
            "limit": PAGE_LIMIT,
        }
        if end_ts is not None:
            params["end"] = end_ts

        try:
            resp = requests.get(BYBIT_KLINE_URL, params=params, timeout=30)
            resp.raise_for_status()
            batch = resp.json().get("result", {}).get("list", [])
        except Exception as exc:  # network / HTTP / JSON — stop, keep what we have
            print(f"    ! request failed ({exc}); stopping pagination")
            break

        if not batch:
            break

        # Bybit returns newest-first: [startTime, open, high, low, close, volume, turnover]
        rows = batch + rows
        end_ts = int(batch[-1][0]) - 1   # step to just before the oldest row

        if len(batch) < PAGE_LIMIT:
            break                        # reached the listing start
        time.sleep(SLEEP_BETWEEN)

    if not rows:
        return pd.DataFrame(columns=["ts", "datetime", "open", "high", "low", "close", "volume"])

    df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume", "turnover"])
    df = df.drop(columns="turnover")
    df["ts"] = df["ts"].astype("int64")
    for col in ("open", "high", "low", "close", "volume"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.drop_duplicates(subset="ts").sort_values("ts").reset_index(drop=True)
    df.insert(1, "datetime", pd.to_datetime(df["ts"], unit="ms", utc=True)
              .dt.strftime("%Y-%m-%dT%H:%M:%SZ"))
    return df


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch Bybit perp OHLCV history for S008.")
    parser.add_argument("--symbols", nargs="+", default=DEFAULT_SYMBOLS,
                        help=f"symbols to fetch (default: {' '.join(DEFAULT_SYMBOLS)})")
    parser.add_argument("--days", type=int, default=2500,
                        help="history depth budget in days (default: 2500 ≈ full perp history)")
    parser.add_argument("--out", type=Path, default=OUT_ROOT,
                        help=f"output root (default: {OUT_ROOT})")
    args = parser.parse_args()

    for symbol in args.symbols:
        sym_dir = args.out / symbol
        sym_dir.mkdir(parents=True, exist_ok=True)
        for interval, stem in TIMEFRAMES.items():
            df = fetch_klines(symbol, interval, args.days)
            out = sym_dir / f"{stem}.csv"
            df.to_csv(out, index=False)
            if len(df):
                print(f"OK   {symbol:9} {stem:3} {len(df):7} candles  "
                      f"{df['datetime'].iloc[0][:10]}..{df['datetime'].iloc[-1][:10]}  -> {out.relative_to(args.out.parent.parent)}")
            else:
                print(f"WARN {symbol:9} {stem:3} no data returned -> {out.name}")


if __name__ == "__main__":
    main()
