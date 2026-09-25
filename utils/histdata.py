"""Shared histdata.com M1 ASCII loader, converted to true UTC.

histdata.com's DAT_ASCII_<SYM>_M1_*.csv files are timestamped on a fixed
UTC-5 (EST) clock year-round, never DST-adjusted -- see scripts/convert_histdata.py,
scripts/convert_histdata_indices.py, and strategies/orb_intraday/engine.py's module
docstring for the project's established, independently-verified convention for
this data source (confirmed by cross-checking trade counts split by EST/EDT
calendar months against an alternative DST-aware loader).

This loader is for strategies specified directly against UTC clock time --
S022/S023/S024's session windows (claude/strategy-passport pending) are all given
in UTC, unlike S021 (strategies/orb_intraday), which deliberately keeps the raw
fixed-EST reading as a stand-in for seasonal NY wall-clock time and does NOT
convert to UTC. Here the conversion is exact and unambiguous: fixed EST-5 IS
UTC-5 with no DST, so UTC = fixed-clock reading + 5h, always -- no seasonal
ambiguity of the kind S021's engine.py documents.

Raw histdata M1 prices are plain decimal (e.g. EUR/USD "1.164410", XAU/USD
"4516.925"), NOT the MT-point-scaled (x1e5 / x1e3) convention used by
scripts/convert_histdata.py's own data/raw/<SYM>/<SYM>m15fresh.csv output --
this loader reads the raw DAT_ASCII files directly and does no scaling, so
callers get native instrument price units throughout (a EUR/USD price like
1.16441, a NAS100 index point like 30484.24).
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

HISTDATA_FIXED_OFFSET = "Etc/GMT+5"  # fixed UTC-5, matches histdata's EST-fixed clock (no DST)


def load_histdata_m1_utc(symbol: str, data_dir: Path) -> pd.DataFrame:
    """Load+concat DAT_ASCII_<symbol>_M1_*.csv from data_dir, return OHLC indexed
    in true UTC (tz-naive, already shifted +5h from the raw fixed-EST reading).

    No look-ahead: this is purely a re-indexing of already-closed M1 bars onto a
    different (but equivalent) clock -- it introduces no future information.
    """
    files = sorted(Path(data_dir).glob(f"DAT_ASCII_{symbol}_M1_*.csv"))
    if not files:
        raise FileNotFoundError(f"No DAT_ASCII_{symbol}_M1_*.csv under {data_dir}")
    frames = []
    for p in files:
        df = pd.read_csv(p, sep=";", header=None,
                          names=["dt", "open", "high", "low", "close", "vol"])
        df["dt"] = pd.to_datetime(df["dt"], format="%Y%m%d %H%M%S")
        frames.append(df)
    m1 = pd.concat(frames).drop_duplicates(subset="dt").set_index("dt").sort_index()
    idx = m1.index.tz_localize(HISTDATA_FIXED_OFFSET).tz_convert("UTC")
    m1.index = idx.tz_localize(None)
    return m1[["open", "high", "low", "close"]].sort_index()


def resample_ohlc(m1: pd.DataFrame, rule: str) -> pd.DataFrame:
    """Resample M1 OHLC to a coarser bar (e.g. '5min', '15min'), label=left,
    closed=left -- the resampled bar's timestamp is its own open time, and it
    only aggregates M1 bars that have already closed by (timestamp + rule)."""
    agg = m1.resample(rule, label="left", closed="left").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last"}
    )
    return agg.dropna(subset=["open", "high", "low", "close"])
