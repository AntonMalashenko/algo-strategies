"""Build data/raw/GER40/GER40m1.csv.gz (local M1 for bot/s007_paper.py --dry-run)
from the GRXEUR M1 files already cached in data/histdata/ by
scripts/fetch_histdata.py + convert_histdata_indices.py.

Same EST(fixed)->Europe/Bucharest conversion as convert_histdata.py, but no
price scaling (real index points, like convert_histdata_indices.py) and kept
at M1 (not resampled), since strategies/ger40_lonfra works off M1 bars.

Usage:
    python scripts/build_ger40_m1.py
"""
from __future__ import annotations

import glob
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
SRC_GLOB = str(ROOT / "data" / "histdata" / "DAT_ASCII_GRXEUR_M1_*.csv")
OUT = ROOT / "data" / "raw" / "GER40" / "GER40m1.csv.gz"


def main():
    files = sorted(glob.glob(SRC_GLOB))
    if not files:
        print(f"No GRXEUR M1 files found under {SRC_GLOB} "
              "-- run scripts/fetch_histdata.py -p grxeur -s 2019-01 first.")
        return

    frames = []
    for f in files:
        df = pd.read_csv(f, sep=";", header=None,
                         names=["dt", "open", "high", "low", "close", "vol"])
        df["dt"] = pd.to_datetime(df["dt"], format="%Y%m%d %H%M%S")
        frames.append(df)
    m1 = pd.concat(frames).set_index("dt").sort_index()
    m1 = m1[~m1.index.duplicated(keep="last")]

    idx = m1.index.tz_localize("Etc/GMT+5").tz_convert("Europe/Bucharest")
    m1.index = idx.tz_localize(None)
    m1 = m1.sort_index()

    out = m1[["open", "high", "low", "close"]].copy()
    out.insert(0, "time", out.index.strftime("%H:%M:%S"))
    out.insert(0, "date", out.index.strftime("%Y-%m-%d"))

    OUT.parent.mkdir(parents=True, exist_ok=True)
    out.to_csv(OUT, index=False, compression="gzip")
    print(f"OK {len(out)} M1 bars  {out['date'].iloc[0]} {out['time'].iloc[0]} .. "
          f"{out['date'].iloc[-1]} {out['time'].iloc[-1]}  -> {OUT}")


if __name__ == "__main__":
    main()
