"""Download Bybit funding-rate history + daily OHLC for the S009 funding-carry study.

S009 harvests perpetual funding (the periodic payment between longs and shorts),
market-neutral. To research it we need, per symbol: the full funding-rate history
and aligned daily prices. Both come from PUBLIC Bybit v5 endpoints (no API key).

A broad universe is used on purpose: a cross-sectional funding carry (short the
highest-funding perps, long the lowest) needs breadth, so we pull ~two dozen
liquid USDT perps rather than only the five from S008. Symbols with no history
are skipped with a warning.

Run from the algo repo root, online (the cloud sandbox has no network):

    python scripts/fetch_funding_bybit.py

Outputs, per symbol, under data/raw/crypto_funding/<SYMBOL>/:
  funding.csv : ts,datetime,funding_rate       (one row per funding stamp)
  d1.csv      : ts,datetime,open,high,low,close,volume

`ts` is epoch-milliseconds UTC. Funding interval varies per symbol (usually 8h);
the timestamps make the actual cadence explicit, so the backtest reads it from
the data rather than assuming.
"""
from __future__ import annotations

import argparse
import time
from pathlib import Path

import pandas as pd
import requests

FUNDING_URL = "https://api.bybit.com/v5/market/funding/history"
KLINE_URL = "https://api.bybit.com/v5/market/kline"

# ~24 liquid Bybit USDT perps for a broad funding cross-section. Missing history
# is skipped, not fatal — the universe can vary period to period.
DEFAULT_UNIVERSE = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT", "ADAUSDT",
    "AVAXUSDT", "LINKUSDT", "DOTUSDT", "LTCUSDT", "TRXUSDT", "ATOMUSDT", "NEARUSDT",
    "APTUSDT", "ARBUSDT", "OPUSDT", "FILUSDT", "INJUSDT", "SUIUSDT", "UNIUSDT",
    "AAVEUSDT", "ETCUSDT", "BCHUSDT",
]

OUT_ROOT = Path(__file__).resolve().parent.parent / "data" / "raw" / "crypto_funding"

PAGE_LIMIT = 200
SLEEP_BETWEEN = 0.15


def fetch_funding(symbol: str) -> pd.DataFrame:
    """Paginate funding history backwards (newest-first) to the listing start."""
    rows: list[dict] = []
    end_ts: int | None = None
    seen: set[int] = set()
    while True:
        params = {"category": "linear", "symbol": symbol, "limit": PAGE_LIMIT}
        if end_ts is not None:
            params["endTime"] = end_ts
        try:
            resp = requests.get(FUNDING_URL, params=params, timeout=30)
            resp.raise_for_status()
            lst = resp.json().get("result", {}).get("list", [])
        except Exception as exc:
            print(f"    ! funding request failed ({exc}); stopping")
            break
        if not lst:
            break
        new = 0
        for r in lst:
            ts = int(r["fundingRateTimestamp"])
            if ts in seen:
                continue
            seen.add(ts); new += 1
            rows.append({"ts": ts, "funding_rate": float(r["fundingRate"])})
        oldest = min(int(r["fundingRateTimestamp"]) for r in lst)
        end_ts = oldest - 1
        if len(lst) < PAGE_LIMIT or new == 0:
            break
        time.sleep(SLEEP_BETWEEN)
    if not rows:
        return pd.DataFrame(columns=["ts", "datetime", "funding_rate"])
    df = pd.DataFrame(rows).drop_duplicates("ts").sort_values("ts").reset_index(drop=True)
    df.insert(1, "datetime", pd.to_datetime(df["ts"], unit="ms", utc=True).dt.strftime("%Y-%m-%dT%H:%M:%SZ"))
    return df


def fetch_daily(symbol: str, days: int = 2600) -> pd.DataFrame:
    """Paginate daily klines backwards to the listing start."""
    target = days
    rows: list[list] = []
    end_ts: int | None = None
    while len(rows) < target:
        params = {"category": "linear", "symbol": symbol, "interval": "D", "limit": 1000}
        if end_ts is not None:
            params["end"] = end_ts
        try:
            resp = requests.get(KLINE_URL, params=params, timeout=30)
            resp.raise_for_status()
            batch = resp.json().get("result", {}).get("list", [])
        except Exception as exc:
            print(f"    ! kline request failed ({exc}); stopping")
            break
        if not batch:
            break
        rows = batch + rows
        end_ts = int(batch[-1][0]) - 1
        if len(batch) < 1000:
            break
        time.sleep(SLEEP_BETWEEN)
    if not rows:
        return pd.DataFrame(columns=["ts", "datetime", "open", "high", "low", "close", "volume"])
    df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume", "turnover"]).drop(columns="turnover")
    df["ts"] = df["ts"].astype("int64")
    for c in ("open", "high", "low", "close", "volume"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.drop_duplicates("ts").sort_values("ts").reset_index(drop=True)
    df.insert(1, "datetime", pd.to_datetime(df["ts"], unit="ms", utc=True).dt.strftime("%Y-%m-%dT%H:%M:%SZ"))
    return df


def main() -> None:
    ap = argparse.ArgumentParser(description="Fetch Bybit funding history + daily OHLC for S009.")
    ap.add_argument("--symbols", nargs="+", default=DEFAULT_UNIVERSE)
    ap.add_argument("--out", type=Path, default=OUT_ROOT)
    args = ap.parse_args()

    for sym in args.symbols:
        d = args.out / sym
        d.mkdir(parents=True, exist_ok=True)
        fund = fetch_funding(sym)
        fund.to_csv(d / "funding.csv", index=False)
        day = fetch_daily(sym)
        day.to_csv(d / "d1.csv", index=False)
        if len(fund) and len(day):
            # infer median funding interval in hours for the log
            gaps = fund["ts"].diff().dropna()
            iv = (gaps.median() / 3_600_000) if len(gaps) else float("nan")
            print(f"OK   {sym:10} funding={len(fund):6d} (~{iv:.0f}h)  "
                  f"{fund['datetime'].iloc[0][:10]}..{fund['datetime'].iloc[-1][:10]}  "
                  f"d1={len(day):5d}")
        else:
            print(f"WARN {sym:10} funding={len(fund)} d1={len(day)} — skipped/empty")


if __name__ == "__main__":
    main()
