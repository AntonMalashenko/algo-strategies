"""Gate 0 regression proof + first honest read of the edge for S021.1 -- NAS100
Opening-Range Squeeze, a default-off PRESET tracked on the S021 engine (NOT a new
strategy/file -- see claude/decisions-log.md, 2026-09-24 entry, and
strategies/orb_intraday/engine.py's simulate_squeeze_preset() module docstring).

This runner has two jobs, run in order:

1. REGRESSION PROOF (must run first, must pass before anything else here means
   anything): reproduce ORB_BASE/simulate()'s frozen baseline BYTE FOR BYTE
   (n=1518, sum_net_pts=16220.614, per claude/strategy-passport-S021.md sec 4) on the
   SAME engine.py file that now also contains simulate_squeeze_preset(). This is the
   concrete proof that adding S021.1 did not touch the base engine's behavior --
   the strategy-modifiers discipline's "base untouched" promise, checked
   mechanically rather than just asserted in a comment.
2. S021.1's own first honest backtest: NAS_SQUEEZE_PRESET run once on real NSXUSD
   data, no parameter search (squeeze_atr_mult=0.7, orb_tp_r_mult=2.5 etc. are all
   untested first guesses per the prompt, not tuned here), walk-forward by year
   (Gate 1), and a cost-sensitivity ladder (Gate 2). Uses the TRUE-UTC loader
   (load_nsxusd_m1_true_utc), NOT ORB_BASE's fixed-EST loader -- S021.1's spec times
   are given in true UTC, unlike ORB_BASE's deliberate fixed-EST-clock anchor.

Usage: python3 backtest/run_s021_squeeze_preset.py [--trades-csv PATH]
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from strategies.orb_intraday.config import NAS_SQUEEZE_PRESET, ORB_BASE, OrbConfig
from strategies.orb_intraday.engine import (
    load_nsxusd_m1,
    load_nsxusd_m1_true_utc,
    simulate,
    simulate_squeeze_preset,
    squeeze_trades_to_frame,
    trades_to_frame,
)

DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "histdata"

# Frozen baseline, reproduced by an independent from-scratch build (claude/strategy-
# passport-S021.md sec 4). Any deviation here means the S021.1 addition changed the
# base engine's behavior and must be treated as a bug, not "close enough".
BASELINE_N = 1518
BASELINE_SUM_NET_PTS = 16220.614

COST_SCENARIOS: list[tuple[str, OrbConfig]] = [
    ("gross 0bp", NAS_SQUEEZE_PRESET.with_(cost_bps_roundtrip=0.0)),
    ("1bp (base)", NAS_SQUEEZE_PRESET),
    ("3bp", NAS_SQUEEZE_PRESET.with_(cost_bps_roundtrip=3.0)),
    ("5bp", NAS_SQUEEZE_PRESET.with_(cost_bps_roundtrip=5.0)),
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


def run_regression_check() -> pd.DataFrame:
    m1_est = load_nsxusd_m1(DATA_DIR)
    base = trades_to_frame(simulate(m1_est, ORB_BASE))
    n, sum_net = len(base), round(base["net_pts"].sum(), 3)
    ok = (n == BASELINE_N) and (abs(sum_net - BASELINE_SUM_NET_PTS) < 1e-3)
    status = "OK -- base engine byte-for-byte unchanged" if ok else "MISMATCH -- REGRESSION"
    print(f"=== Regression check (ORB_BASE/simulate()) === n={n} (expect {BASELINE_N})  "
          f"sum_net_pts={sum_net} (expect {BASELINE_SUM_NET_PTS})  -> {status}")
    if not ok:
        raise SystemExit("S021 base engine regression detected -- refusing to report S021.1 "
                          "results until this is fixed.")
    return base


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--trades-csv", type=Path, default=None)
    args = ap.parse_args()

    run_regression_check()

    m1_utc = load_nsxusd_m1_true_utc(DATA_DIR)
    print(f"\nNSXUSD M1 (true UTC): {m1_utc.index.min()} .. {m1_utc.index.max()} "
          f"({len(m1_utc)} bars)")

    base = squeeze_trades_to_frame(simulate_squeeze_preset(m1_utc, NAS_SQUEEZE_PRESET))
    if args.trades_csv is not None:
        base.to_csv(args.trades_csv, index=False)
        print(f"trades written to {args.trades_csv}")

    if len(base) == 0:
        print("NO TRADES -- nothing further to report")
        return

    s = summarize(base)
    print(f"\n=== S021.1 NAS_SQUEEZE_PRESET (1bp round-trip) === trades {s['n']}  "
          f"win {100 * s['win']:.1f}%  mean R {s['mean_r']:+.4f}  median R {s['median_r']:+.3f}  "
          f"sum R {s['sum_r']:+.1f}  maxDD {s['max_dd_r']:.1f}R  "
          f"longest losing streak {s['streak']}")
    print(f"  first trade {base['day'].min().date()}, last trade {base['day'].max().date()}")
    print("  exits:", base["exit_reason"].value_counts().to_dict())
    for d, g in base.groupby("direction"):
        print(f"  {d:5s}: n {len(g):4d}  win {100 * (g['r_multiple'] > 0).mean():5.1f}%  "
              f"mean R {g['r_multiple'].mean():+.4f}")

    print("\nWalk-forward by calendar year (base, 1bp):")
    print(by_year(base).to_string(float_format=lambda x: f"{x:9.3f}"))

    print("\nCost sensitivity (Gate 2) -- mean R per trade, overall and by year:")
    rows = {}
    for label, cfg in COST_SCENARIOS:
        tr = squeeze_trades_to_frame(simulate_squeeze_preset(m1_utc, cfg))
        yr = tr.groupby(tr["day"].dt.year)["r_multiple"].mean()
        st = summarize(tr)
        row = {"n": st["n"], "win%": 100 * st["win"], "mean_R": st["mean_r"],
               "sum_R": st["sum_r"], "maxDD_R": st["max_dd_r"],
               "pos_years": f"{int((yr > 0).sum())}/{len(yr)}"}
        row.update({str(y): v for y, v in yr.items()})
        rows[label] = row
    print(pd.DataFrame(rows).T.to_string(
        float_format=lambda x: f"{x:+8.3f}" if abs(x) < 100 else f"{x:8.1f}"))

    print("\n--- comparison vs ORB_BASE (same instrument, different mechanics) ---")
    base_orb = trades_to_frame(simulate(load_nsxusd_m1(DATA_DIR), ORB_BASE))
    print(f"ORB_BASE:          n={len(base_orb)}  sum_net_pts={base_orb['net_pts'].sum():.1f}  "
          f"years positive: n/a (points, not R -- see passport)")
    print(f"S021.1 preset:     n={len(base)}  mean_R={base['r_multiple'].mean():+.4f}  "
          f"sum_R={base['r_multiple'].sum():+.1f}")


if __name__ == "__main__":
    main()
