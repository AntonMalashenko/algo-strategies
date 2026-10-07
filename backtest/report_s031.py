"""Aggregate S031 per-symbol trade files: reports/s031_<preset>_<SYM>.csv.

    python -m backtest.report_s031 --preset base
"""
from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
INDICES = {"GRXEUR", "FRXEUR", "UKXGBP", "NSXUSD", "SPXUSD"}


def stats(g: pd.core.groupby.DataFrameGroupBy) -> pd.DataFrame:
    return pd.DataFrame({
        "n": g.size(),
        "win": g["r_net"].apply(lambda s: (s > 0).mean()),
        "tp": g["reason"].apply(lambda s: (s == "tp").mean()),
        "avgR_gross": g["r_gross"].mean(),
        "avgR_net": g["r_net"].mean(),
        "sumR_net": g["r_net"].sum(),
    }).round(3)


def load(preset: str) -> pd.DataFrame:
    files = sorted((ROOT / "reports").glob(f"s031_{preset}_*.csv"))
    df = pd.concat([pd.read_csv(f) for f in files], ignore_index=True)
    df["year"] = pd.to_datetime(df["day"]).dt.year
    df["cls"] = df["symbol"].map(lambda s: "index" if s in INDICES else ("gold" if s == "XAUUSD" else "fx"))
    df["cost_R"] = df["r_gross"] - df["r_net"]
    return df


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--preset", default="base")
    args = ap.parse_args()
    df = load(args.preset)
    pd.set_option("display.width", 220, "display.max_rows", 500)
    print(f"preset={args.preset} trades={len(df)} symbols={df['symbol'].nunique()}")
    print("\n=== model ===\n", stats(df.groupby("model")))
    print("\n=== model x class ===\n", stats(df.groupby(["model", "cls"])))
    yr = df.groupby(["model", "year"])["r_net"].mean().unstack().round(3)
    print("\n=== model x year (avgR net) ===\n", yr)
    sym = df.groupby(["model", "symbol"])["r_net"].mean().unstack(0).round(3)
    print("\n=== symbol x model (avgR net) ===\n", sym)
    pos = (df.groupby(["model", "symbol"])["r_gross"].mean() > 0).groupby("model").sum()
    print("\n=== symbols with positive GROSS avgR, per model ===\n", pos)
    print("\n=== model x with_trend ===\n", stats(df.groupby(["model", "with_trend"])))
    print("\n=== model x level_mirror ===\n", stats(df.groupby(["model", "level_mirror"])))
    print("\n=== model x exit reason ===\n", stats(df.groupby(["model", "reason"])))
    print("\n=== median cost in R per model ===\n", df.groupby("model")["cost_R"].median().round(3))


if __name__ == "__main__":
    main()
