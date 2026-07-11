"""Download 1-minute index data from FutureSharks/financial-data (GitHub)
and aggregate it to M15 in the repo's standard layout.

Run from a machine with internet access (e.g. PyCharm terminal):

    python scripts/download_indices.py                 # default instrument set
    python scripts/download_indices.py --list          # just show what's in the repo
    python scripts/download_indices.py --match spx nas100 grxeur

Output: data/raw/<NAME>/<NAME>m15.csv with columns
Date,open,high,low,close,tick_volume (real prices, not MT points).

Sources inside the repo:
  - histdata  (SPXUSD, GRXEUR=DAX, ETXEUR=EuroStoxx50, JPXJPY=Nikkei), 2010-2018
  - oanda     (SPX500, NAS100, FR40, US2000, AU200, JP225, ...), 2005-2020
Raw 1-minute CSVs are discovered via the GitHub tree API, so exact paths and
naming differences are handled automatically. Unknown file formats are
reported and skipped rather than guessed.
"""
from __future__ import annotations

import argparse
import io
import json
import sys
import time
import urllib.request
from pathlib import Path

import pandas as pd

REPO = "FutureSharks/financial-data"
TREE_URL = f"https://api.github.com/repos/{REPO}/git/trees/master?recursive=1"
RAW_BASE = f"https://raw.githubusercontent.com/{REPO}/master/"
OUT = Path(__file__).resolve().parent.parent / "data" / "raw"
UA = {"User-Agent": "algo-data-downloader"}

# instrument keyword -> output symbol name (distinct from existing daily folders)
DEFAULT_MAP = {
    "spxusd": "SPX500M",     # S&P 500 (histdata)
    "spx500": "SPX500M",     # S&P 500 (oanda)
    "nas100": "NAS100M",     # Nasdaq 100 (oanda)
    "nsxusd": "NAS100M",     # Nasdaq 100 (histdata naming)
    "grxeur": "DAX30M",      # DAX (histdata)
    "de30": "DAX30M",
    "etxeur": "STOXX50M",    # EuroStoxx 50 (histdata)
    "eustx50": "STOXX50M",
    "fr40": "FR40M",         # CAC 40 (oanda)
    "frxeur": "FR40M",
    "us2000": "US2000M",     # Russell 2000 (oanda)
    "uk100": "UK100M",       # FTSE 100 (oanda)
    "ukxgbp": "UK100M",
}


def fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=120) as r:
        return r.read()


def list_tree() -> list[str]:
    data = json.loads(fetch(TREE_URL).decode())
    if data.get("truncated"):
        print("WARNING: GitHub tree listing truncated; some files may be missed")
    return [e["path"] for e in data.get("tree", []) if e["type"] == "blob"]


def parse_minute_csv(blob: bytes, path: str) -> pd.DataFrame | None:
    """Parse a 1-minute file. Supports histdata ASCII (;-separated, no header)
    and generic headered CSVs. Returns df[open, high, low, close, volume]."""
    head = blob[:200].decode("utf-8", errors="replace")
    try:
        if ";" in head.splitlines()[0]:
            df = pd.read_csv(io.BytesIO(blob), sep=";", header=None,
                             names=["dt", "open", "high", "low", "close", "volume"])
            df["dt"] = pd.to_datetime(df["dt"], format="%Y%m%d %H%M%S")
        else:
            df = pd.read_csv(io.BytesIO(blob))
            df.columns = [c.strip().lower() for c in df.columns]
            tcol = next((c for c in df.columns if c in ("date", "time", "datetime", "dt")), None)
            if tcol is None or "close" not in df.columns:
                print(f"  skip (unknown format): {path}")
                return None
            df = df.rename(columns={tcol: "dt"})
            df["dt"] = pd.to_datetime(df["dt"])
            if "volume" not in df.columns:
                df["volume"] = 0
        df = df.set_index("dt").sort_index()
        return df[["open", "high", "low", "close", "volume"]].apply(
            pd.to_numeric, errors="coerce").dropna(subset=["close"])
    except Exception as e:
        print(f"  skip ({e}): {path}")
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--match", nargs="*", default=None,
                    help="instrument keywords (default: built-in index set)")
    ap.add_argument("--list", action="store_true", help="list matching files and exit")
    args = ap.parse_args()

    keymap = DEFAULT_MAP if not args.match else {
        k: DEFAULT_MAP.get(k, k.upper() + "M") for k in [m.lower() for m in args.match]}

    print(f"Listing {REPO} ...")
    paths = list_tree()
    matched: dict[str, list[str]] = {}
    for p in paths:
        pl = p.lower()
        if not pl.endswith(".csv"):
            continue
        for key, sym in keymap.items():
            if key in pl and ("m1" in pl or "/1m" in pl or "minute" in pl or "oanda" in pl):
                matched.setdefault(sym, []).append(p)
                break

    for sym, files in sorted(matched.items()):
        print(f"{sym}: {len(files)} files")
        if args.list:
            for f in sorted(files)[:5]:
                print(f"   {f}")
            if len(files) > 5:
                print(f"   ... and {len(files) - 5} more")
    if args.list or not matched:
        if not matched:
            print("Nothing matched. Try --list with broader --match keywords.")
        return

    for sym, files in sorted(matched.items()):
        frames = []
        for p in sorted(files):
            try:
                blob = fetch(RAW_BASE + urllib.request.quote(p))
            except Exception as e:
                print(f"  FAIL download {p}: {e}")
                continue
            df = parse_minute_csv(blob, p)
            if df is not None and len(df):
                frames.append(df)
            time.sleep(0.15)
        if not frames:
            print(f"{sym}: no usable files, skipped")
            continue
        m1 = pd.concat(frames).sort_index()
        m1 = m1[~m1.index.duplicated(keep="last")]
        agg = {"open": "first", "high": "max", "low": "min",
               "close": "last", "volume": "sum"}
        m15 = m1.resample("15min").agg(agg).dropna(subset=["close"])
        dest = OUT / sym
        dest.mkdir(parents=True, exist_ok=True)
        out = dest / f"{sym}m15.csv"
        m15.index.name = "Date"
        m15 = m15.rename(columns={"volume": "tick_volume"})
        m15.to_csv(out)
        print(f"OK {sym}: {len(m1)} M1 -> {len(m15)} M15 bars "
              f"{m15.index.min()}..{m15.index.max()} -> {out}")


if __name__ == "__main__":
    sys.exit(main())
