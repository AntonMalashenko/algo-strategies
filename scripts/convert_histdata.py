"""Convert histdata.com M1 ASCII files to the repo's M15 format.

Usage (after downloading with histdatacom, see below):

    pip install histdatacom
    histdatacom -p eurusd gbpusd usdjpy usdchf audusd eurjpy gbpjpy \
        -f ascii -t 1-minute-bar-quotes -s 2022-03 -e now
    python scripts/convert_histdata.py [download_dir]

The script recursively finds DAT_ASCII_<SYM>_M1_*.csv files (semicolon-
separated, EST without DST), converts EST -> Europe/Bucharest (EET, to match
the broker-time convention of the 2012-2022 research data), scales prices to
MT points (x1e5, JPY pairs x1e3 => 1 pip = 10 raw units), aggregates M1 to
M15 and writes data/raw/<SYM>/<SYM>m15fresh.csv.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
JPY = {"USDJPY", "EURJPY", "GBPJPY", "AUDJPY"}
PAT = re.compile(r"DAT_ASCII_([A-Z]{6})_M1_\d+", re.IGNORECASE)

# Index CFD symbols (also 6 letters, so they'd otherwise match PAT too) are
# handled separately by convert_histdata_indices.py, with real prices
# instead of MT-point scaling. Keep them out of this FX/metals conversion.
INDEX_SYMBOLS = {"GRXEUR", "FRXEUR", "ETXEUR", "UKXGBP", "NSXUSD", "SPXUSD"}


def main():
    src = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "data"
    files: dict[str, list[Path]] = {}
    for p in src.rglob("*.csv"):
        m = PAT.search(p.name)
        if m and m.group(1).upper() not in INDEX_SYMBOLS:
            files.setdefault(m.group(1).upper(), []).append(p)
    if not files:
        print(f"No DAT_ASCII_*_M1_*.csv files under {src}")
        return

    for sym, paths in sorted(files.items()):
        frames = []
        for p in sorted(paths):
            df = pd.read_csv(p, sep=";", header=None,
                             names=["dt", "open", "high", "low", "close", "vol"])
            df["dt"] = pd.to_datetime(df["dt"], format="%Y%m%d %H%M%S")
            frames.append(df)
        m1 = pd.concat(frames).set_index("dt").sort_index()
        m1 = m1[~m1.index.duplicated(keep="last")]
        # EST fixed (UTC-5, no DST) -> broker time (EET/EEST)
        idx = m1.index.tz_localize("Etc/GMT+5").tz_convert("Europe/Bucharest")
        m1.index = idx.tz_localize(None)
        m1 = m1.sort_index()
        scale = 1e3 if sym in JPY else 1e5
        for c in ["open", "high", "low", "close"]:
            m1[c] = m1[c] * scale
        agg = {"open": "first", "high": "max", "low": "min",
               "close": "last", "vol": "sum"}
        m15 = m1.resample("15min").agg(agg).dropna(subset=["close"])
        m15.index.name = "Date"
        m15 = m15.rename(columns={"vol": "tick_volume"})
        dest = ROOT / "data" / "raw" / sym
        dest.mkdir(parents=True, exist_ok=True)
        out = dest / f"{sym}m15fresh.csv"
        m15.to_csv(out)
        print(f"OK {sym}: {len(m1)} M1 -> {len(m15)} M15  "
              f"{m15.index.min()}..{m15.index.max()}  -> {out}")


if __name__ == "__main__":
    main()
