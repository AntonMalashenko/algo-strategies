"""S031 backtest runner: every model x every instrument, course-literal config.

Usage:
    python -m backtest.run_s031_gerchik --preset base [--symbols EURUSD,XAUUSD] [--models fb2,fb3]

Writes reports/s031_trades_<preset>.csv (all trades) and prints per-model,
per-year and per-instrument summaries (gross and net R).
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from strategies.gerchik_levels.config import (ALL_MODELS, BASE_S031, INSTRUMENTS, NO_TREND_S031,  # noqa: E402
                                              STOP_ATR_S031)
from strategies.gerchik_levels.engine import simulate, trades_to_frame  # noqa: E402
from utils.histdata import load_histdata_m1_utc  # noqa: E402

PRESETS = {"base": BASE_S031, "stop_atr": STOP_ATR_S031, "no_trend": NO_TREND_S031}
DATA_DIR = ROOT / "data" / "histdata"
REPORTS = ROOT / "reports"


def summarize(df: pd.DataFrame, by: list[str]) -> pd.DataFrame:
    g = df.groupby(by)
    return pd.DataFrame({
        "n": g.size(),
        "win": g["r_net"].apply(lambda s: (s > 0).mean()),
        "avgR_gross": g["r_gross"].mean(),
        "avgR_net": g["r_net"].mean(),
        "sumR_net": g["r_net"].sum(),
        "tp_share": g["reason"].apply(lambda s: (s == "tp").mean()),
    }).round(3)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--preset", default="base", choices=sorted(PRESETS))
    ap.add_argument("--symbols", default=",".join(INSTRUMENTS))
    ap.add_argument("--models", default=",".join(ALL_MODELS))
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    base = PRESETS[args.preset]
    frames = []
    for sym in args.symbols.split(","):
        t0 = time.time()
        m1 = load_histdata_m1_utc(sym, DATA_DIR)
        for model in args.models.split(","):
            tr = trades_to_frame(simulate(sym, m1, base.with_(model=model)))
            frames.append(tr)
            print(f"{sym:7s} {model:8s} n={len(tr):5d}", flush=True)
        print(f"  {sym} done in {time.time() - t0:.0f}s", flush=True)
    df = pd.concat([f for f in frames if len(f)], ignore_index=True)
    out = Path(args.out) if args.out else REPORTS / f"s031_trades_{args.preset}.csv"
    df.to_csv(out, index=False)
    df["year"] = pd.to_datetime(df["day"]).dt.year
    pd.set_option("display.width", 200)
    print("\n=== by model ===\n", summarize(df, ["model"]))
    print("\n=== by model x year ===\n", summarize(df, ["model", "year"]))
    print("\n=== by model x symbol ===\n", summarize(df, ["model", "symbol"]))
    print(f"\nwritten {out} ({len(df)} trades)")


if __name__ == "__main__":
    main()
