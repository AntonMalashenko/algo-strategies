"""Build data/GER40/GER40_Dukascopy_M1_<start>_<end>.csv.gz from a raw
dukascopy-node CSV export (data/fresh/deuidxeur-m1-bid-*.csv).

The upstream file (`npx dukascopy-node -i deuidxeur -from ... -to now -t m1
-f csv -v true -dir data/fresh`) has UTC epoch-ms timestamps and a
`timestamp,open,high,low,close,volume` header. strategies/ger40_lonfra/data.py
(the 'duka' loader) expects `date,time,open,high,low,close` with the bars
already in Kyiv/EET local time (see strategy-spec-S007.md sec 0) -- this
script does exactly that conversion, no price scaling (index already in real
points), and drops the tick-volume column (unused by the engine).

Usage:
    python3 scripts/build_ger40_dukascopy_m1.py
"""
from __future__ import annotations

import glob
import os
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
SRC_GLOB = str(ROOT / "data" / "fresh" / "deuidxeur-m1-bid-*.csv")
OUT_DIR = ROOT / "data" / "GER40"


def main():
    files = sorted(glob.glob(SRC_GLOB))
    if not files:
        print(f"No dukascopy-node export found under {SRC_GLOB} -- run:\n"
              "  npx dukascopy-node -i deuidxeur -from 2023-06-26 -to now -t m1 "
              "-f csv -v true -dir data/fresh")
        return
    src = files[-1]  # most recent export if more than one
    print(f"Reading {src} ...")
    df = pd.read_csv(src)
    df.columns = [c.strip().lower() for c in df.columns]

    dt_utc = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    dt_kyiv = dt_utc.dt.tz_convert(ZoneInfo("Europe/Kyiv")).dt.tz_localize(None)

    out = pd.DataFrame({
        "date": dt_kyiv.dt.strftime("%Y-%m-%d"),
        "time": dt_kyiv.dt.strftime("%H:%M:%S"),
        "open": df["open"], "high": df["high"], "low": df["low"], "close": df["close"],
    })
    out = out.sort_values(["date", "time"]).drop_duplicates(subset=["date", "time"], keep="last")

    start, end = out["date"].iloc[0], out["date"].iloc[-1]
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUT_DIR / f"GER40_Dukascopy_M1_{start}_{end}.csv.gz"
    out.to_csv(out_path, index=False, compression="gzip")
    print(f"OK {len(out)} M1 bars, {start} .. {end} (Kyiv local time) -> {out_path}")


if __name__ == "__main__":
    main()
