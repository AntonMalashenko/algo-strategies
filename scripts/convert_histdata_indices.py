"""Convert histdata.com M1 ASCII index/CFD files to the repo's M15 format.

Companion to convert_histdata.py, but for indices: unlike FX pairs, index
CFD prices are used as-is (no MT-point scaling) to match the convention of
the existing data/raw/<NAME>/<NAME>m15.csv files (sourced from
FutureSharks/financial-data, see scripts/download_indices.py). Timestamps
are also left in histdata.com's native EST-fixed (no DST) convention,
matching the DAX30M/STOXX50M files already in the repo, which come from the
same histdata.com source. This differs from the oanda-sourced portions of
FR40M/NAS100M/SPX500M/UK100M (likely UTC-ish) -- there is a possible
timezone offset of a few hours at the seam where old and new data meet.

Usage (after downloading with scripts/fetch_histdata.py):

    python scripts/fetch_histdata.py -p grxeur frxeur etxeur ukxgbp nsxusd spxusd -s 2019-01
    python scripts/convert_histdata_indices.py [download_dir]

Output: data/raw/<NAME>/<NAME>m15fresh.csv
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
PAT = re.compile(r"DAT_ASCII_([A-Z]{6})_M1_\d+", re.IGNORECASE)

# histdata.com CFD symbol -> repo index folder name (matches
# scripts/download_indices.py DEFAULT_MAP). No histdata.com equivalent for
# US2000M (Russell 2000).
INDEX_MAP = {
    "GRXEUR": "DAX30M",
    "FRXEUR": "FR40M",
    "ETXEUR": "STOXX50M",
    "UKXGBP": "UK100M",
    "NSXUSD": "NAS100M",
    "SPXUSD": "SPX500M",
}


def main():
    src = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "data"
    files: dict[str, list[Path]] = {}
    for p in src.rglob("*.csv"):
        m = PAT.search(p.name)
        if m and m.group(1).upper() in INDEX_MAP:
            files.setdefault(m.group(1).upper(), []).append(p)
    if not files:
        print(f"No DAT_ASCII_*_M1_*.csv index files under {src}")
        return

    for sym, paths in sorted(files.items()):
        name = INDEX_MAP[sym]
        frames = []
        for p in sorted(paths):
            df = pd.read_csv(p, sep=";", header=None,
                             names=["dt", "open", "high", "low", "close", "vol"])
            df["dt"] = pd.to_datetime(df["dt"], format="%Y%m%d %H%M%S")
            frames.append(df)
        m1 = pd.concat(frames).set_index("dt").sort_index()
        m1 = m1[~m1.index.duplicated(keep="last")]
        agg = {"open": "first", "high": "max", "low": "min",
               "close": "last", "vol": "sum"}
        m15 = m1.resample("15min").agg(agg).dropna(subset=["close"])
        m15.index.name = "Date"
        m15 = m15.rename(columns={"vol": "tick_volume"})
        dest = ROOT / "data" / "raw" / name
        dest.mkdir(parents=True, exist_ok=True)
        out = dest / f"{name}m15fresh.csv"
        m15.to_csv(out)
        print(f"OK {sym} -> {name}: {len(m1)} M1 -> {len(m15)} M15  "
              f"{m15.index.min()}..{m15.index.max()}  -> {out}")


if __name__ == "__main__":
    main()
