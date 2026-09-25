"""Gate 0 proof-of-life + Gate 1 (walk-forward-by-year) + Gate 2 (cost
sensitivity) first honest read of S022 -- Asia Mean Reversion, EUR/USD.

Not a prop-firm/Gate 3 simulation (that's a later step, once/if this survives
Gates 1-2) -- see strategies/asia_mean_reversion.py's module docstring for the
full rule set and the judgment calls made to turn Anton's spec into mechanical
rules (VWAP proxy, entry timing, stop-first ordering, etc).

Usage: python3 backtest/run_asia_mean_reversion.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from strategies.asia_mean_reversion import ASIA_MR_BASE, simulate, trades_to_frame
from utils.histdata import load_histdata_m1_utc, resample_ohlc

DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "histdata"
SYMBOL = "EURUSD"


def summarize(trades: pd.DataFrame, cost_bps: float, entry_price_col: str = "entry_price") -> dict:
    if trades.empty:
        return {"n": 0, "win_rate": float("nan"), "mean_r": float("nan"), "sum_r": 0.0}
    cost = trades[entry_price_col] * cost_bps / 10_000.0
    net = trades["gross"] - cost
    stop_dist = (trades["entry_price"] - trades["stop_price"]).abs()
    r = net / stop_dist.replace(0.0, np.nan)
    return {
        "n": len(trades),
        "win_rate": float((net > 0).mean()),
        "mean_r": float(r.mean()),
        "sum_r": float(r.sum()),
    }


def main():
    m1 = load_histdata_m1_utc(SYMBOL, DATA_DIR)
    m5 = resample_ohlc(m1, "5min")
    print(f"{SYMBOL} M5 UTC: {m5.index.min()} .. {m5.index.max()}  ({len(m5)} bars)")

    trades = trades_to_frame(simulate(m5, ASIA_MR_BASE))
    print(f"\nTotal trades (frozen base config, 1bp round-trip already baked into 'net'/'r_multiple'): {len(trades)}")
    if trades.empty:
        print("No trades produced -- nothing further to report.")
        return

    print(f"Direction split: long={sum(trades.direction=='long')} short={sum(trades.direction=='short')}")
    print(f"Exit reasons: {trades['exit_reason'].value_counts().to_dict()}")
    print(f"Win rate (net>0): {(trades['net'] > 0).mean():.3f}")
    print(f"Mean R: {trades['r_multiple'].mean():.4f}  Median R: {trades['r_multiple'].median():.4f}  Sum R: {trades['r_multiple'].sum():.2f}")

    # ---- Gate 1: walk-forward by calendar year ----
    print("\n--- Gate 1: walk-forward by year (mean R, trade count) ---")
    trades["year"] = trades["entry_time"].dt.year
    by_year = trades.groupby("year")["r_multiple"].agg(["count", "mean", "sum"])
    print(by_year.to_string(float_format=lambda x: f"{x:.4f}"))
    positive_years = int((by_year["mean"] > 0).sum())
    print(f"Positive years: {positive_years}/{len(by_year)}")

    # ---- Gate 2: cost sensitivity ----
    # Each rerun uses cfg.cost_bps_roundtrip, which simulate() already bakes
    # into 'net'/'r_multiple' -- read those columns directly rather than via
    # summarize() (which independently re-derives cost from 'gross' and would
    # double up / ignore the cfg's own cost if reused here).
    print("\n--- Gate 2: cost sensitivity (re-simulate at each cost level) ---")
    for cost_bps in (0.0, 1.0, 3.0, 5.0):
        cfg = ASIA_MR_BASE.with_(cost_bps_roundtrip=cost_bps)
        t = trades_to_frame(simulate(m5, cfg))
        if t.empty:
            print(f"  cost={cost_bps:>4.1f}bp  n=0")
            continue
        win_rate = float((t["net"] > 0).mean())
        mean_r = float(t["r_multiple"].mean())
        sum_r = float(t["r_multiple"].sum())
        by_year_c = t.assign(year=t["entry_time"].dt.year).groupby("year")["r_multiple"].mean()
        pos = int((by_year_c > 0).sum())
        print(f"  cost={cost_bps:>4.1f}bp  n={len(t):>4}  win_rate={win_rate:.3f}  "
              f"mean_R={mean_r:+.4f}  sum_R={sum_r:+.1f}  positive_years={pos}/{len(by_year_c)}")


if __name__ == "__main__":
    main()
