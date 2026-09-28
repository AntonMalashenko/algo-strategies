"""Gate 0 proof-of-life + first honest read of the edge for S022.1 -- Asia Sweep &
Reversion, EUR/USD M5, sweep-and-reclaim of the 00:00-06:55 UTC Asian range in the
07:00-09:30 UTC European open (strategy-lifecycle skill sec "validation gates";
spec claude/prompts-new-strategy-candidates.md P14; engine
strategies/asia_sweep_reversion.py).

This is the first pass that answers "does the frozen spec, run once on real data
with no parameter search, show any edge at all, is it stable across years, and
does it survive realistic FX costs" -- same shape as
backtest/run_gold_session_momentum.py and backtest/run_asia_mean_reversion.py:

* Data: histdata.com EURUSD M1 (same source/instrument as S022, for continuity),
  loaded and shifted to true UTC by utils.histdata.load_histdata_m1_utc, resampled
  to M5 (label=left) -- same M5 granularity as S022.
* Config: ASIA_SWEEP_BASE only, exactly as specified. No parameter is swept or
  tuned here, on purpose -- sweep_buffer_price and tp_r_mult are both explicitly
  flagged in the engine docstring as honest first guesses, not optimized values;
  this run is what tells us whether they need revisiting, not a search for better
  ones.
* R-multiple: R = net / stop_dist, stop_dist = |entry - stop| (sweep extreme +/-
  buffer), same convention as S022/S023/S024.
* Walk-forward by calendar year (Gate 1 requirement: positive in EVERY year).
* Cost sensitivity (Gate 2): 0bp (reference), 1bp (frozen baseline), 3bp, 5bp
  (stressed) -- same scenario ladder as S022/S023/S024, bps of entry price.

KNOWN OPTIMISTIC BIASES (not corrected here, see the engine docstring): SL/TP fill
exactly at their level (no gap-through slippage); sweep_buffer_price/tp_r_mult are
untuned first guesses, not a measured/optimized figure.

Usage: python3 backtest/run_asia_sweep_reversion.py [--trades-csv PATH]
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from strategies.asia_sweep_reversion import (ASIA_SWEEP_BASE, AsiaSweepConfig,
                                             simulate, trades_to_frame)
from utils.histdata import load_histdata_m1_utc, resample_ohlc

DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "histdata"
SYMBOL = "EURUSD"
BAR_RULE = "5min"

COST_SCENARIOS: list[tuple[str, AsiaSweepConfig]] = [
    ("gross 0bp", ASIA_SWEEP_BASE.with_(cost_bps_roundtrip=0.0)),
    ("1bp (base)", ASIA_SWEEP_BASE),
    ("3bp", ASIA_SWEEP_BASE.with_(cost_bps_roundtrip=3.0)),
    ("5bp", ASIA_SWEEP_BASE.with_(cost_bps_roundtrip=5.0)),
]


def max_drawdown_r(r: np.ndarray) -> float:
    if len(r) == 0:
        return 0.0
    cum = np.cumsum(r)
    peak = np.maximum.accumulate(np.concatenate([[0.0], cum]))[1:]
    return float((cum - peak).min())


def longest_losing_streak(r: np.ndarray) -> int:
    streak = best = 0
    for x in r:
        streak = streak + 1 if x < 0 else 0
        best = max(best, streak)
    return best


def summarize(tr: pd.DataFrame) -> dict:
    r = tr["r_multiple"].to_numpy()
    return dict(n=len(tr), win=float((r > 0).mean()) if len(r) else np.nan,
                mean_r=float(r.mean()) if len(r) else np.nan,
                median_r=float(np.median(r)) if len(r) else np.nan,
                sum_r=float(r.sum()), max_dd_r=max_drawdown_r(r),
                streak=longest_losing_streak(r))


def by_year(tr: pd.DataFrame) -> pd.DataFrame:
    g = tr.groupby(tr["day"].dt.year)
    out = pd.DataFrame({
        "n": g.size(),
        "win%": 100 * g["r_multiple"].apply(lambda s: (s > 0).mean()),
        "mean_R": g["r_multiple"].mean(),
        "sum_R": g["r_multiple"].sum(),
        "tp%": 100 * g["exit_reason"].apply(lambda s: (s == "tp").mean()),
    })
    out.index.name = "year"
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--trades-csv", type=Path, default=None)
    args = ap.parse_args()

    m1 = load_histdata_m1_utc(SYMBOL, DATA_DIR)
    m5 = resample_ohlc(m1, BAR_RULE)
    print(f"{SYMBOL} M1 (UTC): {m1.index.min()} .. {m1.index.max()} ({len(m1)} bars) "
          f"-> M5 {len(m5)} bars")

    base = trades_to_frame(simulate(m5, ASIA_SWEEP_BASE))
    if args.trades_csv is not None:
        base.to_csv(args.trades_csv, index=False)
        print(f"trades written to {args.trades_csv}")

    if len(base) == 0:
        print("NO TRADES -- nothing further to report")
        return

    s = summarize(base)
    print(f"\n=== BASE (1bp round-trip) === trades {s['n']}  win {100 * s['win']:.1f}%  "
          f"mean R {s['mean_r']:+.4f}  median R {s['median_r']:+.3f}  sum R {s['sum_r']:+.1f}  "
          f"maxDD {s['max_dd_r']:.1f}R  longest losing streak {s['streak']}")
    print(f"  first trade {base['day'].min().date()}, last trade {base['day'].max().date()}")
    print("  exits:", base["exit_reason"].value_counts().to_dict())
    print("  tp_candidate:", base["tp_candidate"].value_counts().to_dict())
    for d, g in base.groupby("direction"):
        print(f"  {d:5s}: n {len(g):4d}  win {100 * (g['r_multiple'] > 0).mean():5.1f}%  "
              f"mean R {g['r_multiple'].mean():+.4f}")
    for reason, g in base.groupby("exit_reason"):
        print(f"  exit={reason:4s}: n {len(g):4d}  mean R {g['r_multiple'].mean():+.3f}")

    print("\nWalk-forward by calendar year (base, 1bp):")
    print(by_year(base).to_string(float_format=lambda x: f"{x:9.3f}"))

    print("\nCost sensitivity (Gate 2) -- mean R per trade, overall and by year:")
    rows = {}
    for label, cfg in COST_SCENARIOS:
        tr = trades_to_frame(simulate(m5, cfg))
        yr = tr.groupby(tr["day"].dt.year)["r_multiple"].mean()
        st = summarize(tr)
        row = {"n": st["n"], "win%": 100 * st["win"], "mean_R": st["mean_r"],
               "sum_R": st["sum_r"], "maxDD_R": st["max_dd_r"],
               "pos_years": f"{int((yr > 0).sum())}/{len(yr)}"}
        row.update({str(y): v for y, v in yr.items()})
        rows[label] = row
    print(pd.DataFrame(rows).T.to_string(
        float_format=lambda x: f"{x:+8.3f}" if abs(x) < 100 else f"{x:8.1f}"))


if __name__ == "__main__":
    main()
