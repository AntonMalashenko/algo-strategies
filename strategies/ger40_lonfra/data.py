"""Data loading and daily reference levels for GER40 M1.

Loaders are copied verbatim (semantically) from the reference scripts so the
consolidated engine reads the exact same bars. Two sources:
  - 'duka' : Dukascopy bid M1, gzipped CSV, columns date,time,open,high,low,close
  - 'mt5'  : MT5 export, tab-separated, <DATE>/<TIME>/... columns
Both are already in Kyiv time and globally timestamp-sorted.
"""
from __future__ import annotations

import gzip
import os

import numpy as np
import pandas as pd

# Resolve data dir: prefer a local checkout layout, fall back to the Cowork
# upload mount (same trick the reference pyramid_v2.py uses).
_CANDIDATES = [
    os.environ.get("GER40_DATA_DIR", ""),
    os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "..", "data", "GER40")),
    "/mnt/user-data/uploads/strategies/data/GER40",
]


def _data_dir() -> str:
    for d in _CANDIDATES:
        if d and os.path.isdir(d):
            return d
    raise FileNotFoundError(
        "GER40 data dir not found; set GER40_DATA_DIR to the folder holding the "
        "Dukascopy/MT5 CSVs."
    )


DUKA_NAME = "GER40_Dukascopy_M1_2023-06-26_2026-07-13.csv.gz"
MT5_NAME = "GER40.cash_M1_202603231944_202607082311.csv"


def load(source: str) -> pd.DataFrame:
    """Return an M1 DataFrame with dt, date_only, time_only helper columns."""
    d = _data_dir()
    if source == "mt5":
        df = pd.read_csv(os.path.join(d, MT5_NAME), sep="\t")
        df.columns = [c.strip("<>").lower() for c in df.columns]
        df["dt"] = pd.to_datetime(
            df["date"].str.replace(".", "-", regex=False) + " " + df["time"]
        )
    elif source == "duka":
        with gzip.open(os.path.join(d, DUKA_NAME), "rt") as f:
            df = pd.read_csv(f)
        df.columns = [c.lower() for c in df.columns]
        df["dt"] = pd.to_datetime(df["date"] + " " + df["time"])
    else:
        raise ValueError(f"unknown source {source!r} (use 'duka' or 'mt5')")
    df = df.sort_values("dt").reset_index(drop=True)
    df["date_only"] = df["dt"].dt.date
    df["time_only"] = df["dt"].dt.strftime("%H:%M")
    return df


def daily_levels(df: pd.DataFrame) -> dict:
    """Prior-day RTH high/low and Asian high/low per date (liquidity proxies).

    Verbatim logic from pyramid_v2.daily_levels: RTH = 09:00-17:29,
    Asia = 02:00-08:59. Prior-day extremes are shifted by one trading date, so
    there is no intraday look-ahead.
    """
    lv: dict = {}
    rth = df[(df["time_only"] >= "09:00") & (df["time_only"] <= "17:29")]
    dh = rth.groupby("date_only")["high"].max()
    dl = rth.groupby("date_only")["low"].min()
    asia = df[(df["time_only"] >= "02:00") & (df["time_only"] <= "08:59")]
    ah = asia.groupby("date_only")["high"].max()
    al = asia.groupby("date_only")["low"].min()
    dates = sorted(df["date_only"].unique())
    prevh: dict = {}
    prevl: dict = {}
    for i, dd in enumerate(dates):
        if i > 0:
            pv = dates[i - 1]
            prevh[dd] = dh.get(pv, np.nan)
            prevl[dd] = dl.get(pv, np.nan)
        else:
            prevh[dd] = np.nan
            prevl[dd] = np.nan
    for dd in dates:
        lv[dd] = dict(
            prev_day_high=prevh.get(dd, np.nan),
            prev_day_low=prevl.get(dd, np.nan),
            asia_high=ah.get(dd, np.nan),
            asia_low=al.get(dd, np.nan),
        )
    return lv
