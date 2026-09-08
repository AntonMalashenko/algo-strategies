"""Dataset builder for the S007 meta-labeling experiment (ALGODEV-14, P2).

Applies the S004 meta-labeling methodology (backtest/s004_metalabel_data.py)
to S007. Builds one row per S007 traded DAY with causal features computed
strictly from information available at the first entry time, plus the realized
outcome (day_R, win).

Unit of learning: the DAY, not the individual add. S007's pyramided positions
share one common mid_range stop and one common day TP, so add outcomes within
a day are almost perfectly dependent -- labeling adds individually would
manufacture pseudo-replicated samples (see s007_metalabel_eval.py, which
quantifies this dependence on the actual positions list). Daily aggregation
of R is also what the S007 passport reports, so day-level labels keep the
results directly comparable.

Not a trading-decision change: reuses strategies.ger40_lonfra.engine.run
unmodified. This script only shapes its per-day output into an ML-ready table.

Usage: python3 backtest/s007_metalabel_data.py [--preset liqfloor|newssafe]
                                               [--until YYYY-MM-DD] [--out PATH]
  --until truncates the input M1 data before running the backtest, used by
  the Gate-0 no-look-ahead check (s007_metalabel_gate0.py).
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from strategies.ger40_lonfra import config as C
from strategies.ger40_lonfra import data as D
from strategies.ger40_lonfra.engine import run

ROOT = Path(__file__).resolve().parent.parent
REAL_SPREAD_PER_SIDE = 0.635  # Gate 2 standard, strategy-spec-S007 sec 10.2 (1.27pt RT / 2)
TREND_MA_DAYS = 20            # matches the S004/S016 precedent for daily trend context
ATR_WINDOW = 14               # standard ATR window (daily)
RTH_START, RTH_END = "09:00", "17:29"   # engine's own RTH definition (gap filter)
ASIA_START, ASIA_END = "02:00", "08:59"  # data.daily_levels convention

PRESETS = {
    "liqfloor": ("WORKING_S007_LIQFLOOR", C.WORKING_S007_LIQFLOOR),
    "newssafe": ("WORKING_S007_NEWSSAFE", C.WORKING_S007_NEWSSAFE),
}


def load_m1(until: str | None = None) -> pd.DataFrame:
    df = D.load("duka")
    if until is not None:
        df = df[df["dt"] <= pd.Timestamp(until)].reset_index(drop=True)
    return df


def _daily_context(df: pd.DataFrame) -> pd.DataFrame:
    """Per-date PRIOR-day context (shifted by one trading date, so a value for
    date d only uses data from dates < d -- same causality convention as the
    engine's own gap filter and data.daily_levels)."""
    rth = df[(df["time_only"] >= RTH_START) & (df["time_only"] <= RTH_END)]
    day = rth.groupby("date_only").agg(
        high=("high", "max"), low=("low", "min"),
        close=("close", "last"), open=("open", "first"))
    prev_close = day["close"].shift(1)
    tr = pd.concat([
        day["high"] - day["low"],
        (day["high"] - prev_close).abs(),
        (day["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    atr = tr.rolling(ATR_WINDOW).mean()
    sma = day["close"].rolling(TREND_MA_DAYS).mean()
    ctx = pd.DataFrame({
        # .shift(1): only the PREVIOUS completed day's stats are known at entry
        "prev_rth_close": day["close"].shift(1),
        "prev_atr_pct": (atr / day["close"]).shift(1),
        "prev_trend_dir": np.sign(day["close"] - sma).shift(1),
        "prev_day_range_pct": ((day["high"] - day["low"]) / day["close"]).shift(1),
    })
    ctx.index.name = "date"
    return ctx


def _asia_range(df: pd.DataFrame) -> pd.Series:
    """Same-day Asia-session (02:00-08:59) range in points. Causal: the Asia
    session ends before the Frankfurt hour, which ends before any entry."""
    asia = df[(df["time_only"] >= ASIA_START) & (df["time_only"] <= ASIA_END)]
    g = asia.groupby("date_only")
    return g["high"].max() - g["low"].min()


def _london_open(df: pd.DataFrame, trade_start: str) -> pd.Series:
    lo = df[df["time_only"] == trade_start].groupby("date_only")["open"].first()
    return lo


def build_features(df: pd.DataFrame, res: pd.DataFrame, trade_start: str) -> pd.DataFrame:
    if res.empty:
        return res
    ctx = _daily_context(df)
    asia_rng = _asia_range(df)
    lopen = _london_open(df, trade_start)

    r = res.copy()
    r["date"] = pd.to_datetime(r["date"].astype(str))
    key = r["date"].dt.date

    r["prev_atr_pct"] = key.map(ctx["prev_atr_pct"]).astype(float)
    r["prev_trend_dir"] = key.map(ctx["prev_trend_dir"]).fillna(0.0).astype(float)
    r["prev_day_range_pct"] = key.map(ctx["prev_day_range_pct"]).astype(float)
    prev_rth_close = key.map(ctx["prev_rth_close"]).astype(float)
    london_open = key.map(lopen).astype(float)
    r["gap_points"] = (london_open - prev_rth_close).abs()
    r["gap_frac_height"] = r["gap_points"] / r["height"]
    r["asia_range_points"] = key.map(asia_rng).astype(float)
    r["asia_range_frac_height"] = r["asia_range_points"] / r["height"]

    # engine-row features, all fixed at (or before) the first entry
    r["is_B"] = (r["scenario"] == "B").astype(int)
    r["dir_up"] = (r["direction"] == "up").astype(int)
    r["entry_minute"] = (
        pd.to_datetime(r["entry_time"], format="%H:%M")
        - pd.to_datetime(trade_start, format="%H:%M")
    ).dt.total_seconds() / 60.0
    r["height_bps"] = r["height"] / r["entry_price"] * 1e4
    r["first_risk_frac_height"] = r["first_risk"] / r["height"]
    r["dist_to_tp_R"] = (r["tp"] - r["entry_price"]).abs() / r["first_risk"]
    r["entry_pos_in_range"] = (r["entry_price"] - r["rl"]) / r["height"]
    r["open_above_mid"] = r["open_above_mid"].astype(int)
    r["dow"] = r["date"].dt.dayofweek

    r["win"] = (r["day_R"] > 0).astype(int)
    return r


FEATURE_COLS = [
    "is_B", "dir_up", "entry_minute", "height_bps",
    "first_risk_frac_height", "dist_to_tp_R", "entry_pos_in_range",
    "open_above_mid", "dow",
    "gap_frac_height", "asia_range_frac_height",
    "prev_atr_pct", "prev_trend_dir", "prev_day_range_pct",
]
KEEP_COLS = ["date", "scenario", "direction", "height", "entry_time", "entry_price",
             "first_risk", "tp", "n_pos", "n_recovery", "day_R", "win"]


def build_all(preset: str, until: str | None = None,
              df: pd.DataFrame | None = None) -> pd.DataFrame:
    name, base_cfg = PRESETS[preset]
    cfg = base_cfg.with_(spread_per_side=REAL_SPREAD_PER_SIDE)
    if df is None:
        df = load_m1(until=until)
    lv = D.daily_levels(df)
    res = run(df, cfg, lv)
    out = build_features(df, res, cfg.trade_start)
    print(f"{name}: {df['date_only'].min()}..{df['date_only'].max()}, "
          f"{len(out)} traded days, net {out['day_R'].mean():+.4f} R/day")
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--preset", choices=sorted(PRESETS), default="liqfloor")
    ap.add_argument("--until", default=None)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    tbl = build_all(args.preset, until=args.until)
    out_path = (Path(args.out) if args.out
                else ROOT / "reports" / f"s007_metalabel_dataset_{args.preset}.csv")
    tbl[KEEP_COLS + FEATURE_COLS].to_csv(out_path, index=False)
    print(f"Total: {len(tbl)} days -> {out_path}")
    print(tbl[["day_R", "win"] + FEATURE_COLS].describe().T[["mean", "std", "min", "max"]])
