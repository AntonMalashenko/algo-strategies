"""Gate 0 proof-of-life + first honest read of the edge for S024.1 -- Gold
London-Range Breakout with Chandelier Trail, XAU/USD M15, fixed London-session
range (07:00-12:30 UTC) breakout with a Chandelier trailing exit instead of
S024's static TP (strategy-lifecycle skill sec "validation gates"; spec
claude/prompts-new-strategy-candidates.md P14; engine
strategies/gold_chandelier_breakout.py).

Same shape as backtest/run_gold_session_momentum.py -- this is the first pass
that answers "does the frozen spec, run once on real data with no parameter
search, show any edge at all, is it stable across years, and does it survive
realistic gold CFD costs", NOT a parameter search:

* Data: histdata.com XAUUSD M1 (same source/instrument as S024), loaded and
  shifted to true UTC by utils.histdata.load_histdata_m1_utc, resampled to M15
  -- same granularity as S024, for direct comparability.
* Config: GOLD_CHANDELIER_BASE only, exactly as specified. No parameter
  (trigger/trail multiples, EMA/ATR periods) is swept or tuned here, on
  purpose -- same "first honest look, not a search for a better one" principle
  as every other engine in this project.
* R-multiple: R = net / stop_dist, stop_dist = 1.5xATR(14) at the signal bar
  (the ORIGINAL risk unit, frozen at entry -- NOT the trailing distance),
  same convention as S024.
* Walk-forward by calendar year (Gate 1: positive in EVERY year).
* Cost sensitivity (Gate 2): same scenario ladder as S024 (0bp, 1bp, 3bp, 5bp,
  $0.30/oz, $0.60/oz).

KNOWN OPTIMISTIC BIASES (not corrected here, see the engine docstring): entry
fills at the breakout bar's own close; SL/trail fill exactly at their level (no
gap-through slippage); trigger/trail multiples (1.0x / 2.0x ATR) are untuned
first guesses from the prompt, not measured/optimized figures.

Usage: python3 backtest/run_gold_chandelier_breakout.py [--trades-csv PATH]
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from strategies.gold_chandelier_breakout import (GOLD_CHANDELIER_BASE, GoldChandelierConfig,
                                                  simulate, trades_to_frame)
from utils.histdata import load_histdata_m1_utc, resample_ohlc

DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "histdata"
SYMBOL = "XAUUSD"
BAR_RULE = "15min"

COST_SCENARIOS: list[tuple[str, GoldChandelierConfig]] = [
    ("gross 0bp", GOLD_CHANDELIER_BASE.with_(cost_bps_roundtrip=0.0)),
    ("1bp (base)", GOLD_CHANDELIER_BASE),
    ("3bp", GOLD_CHANDELIER_BASE.with_(cost_bps_roundtrip=3.0)),
    ("5bp", GOLD_CHANDELIER_BASE.with_(cost_bps_roundtrip=5.0)),
    ("$0.30/oz", GOLD_CHANDELIER_BASE.with_(cost_bps_roundtrip=0.0, cost_price_roundtrip=0.30)),
    ("$0.60/oz", GOLD_CHANDELIER_BASE.with_(cost_bps_roundtrip=0.0, cost_price_roundtrip=0.60)),
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
        "trail%": 100 * g["exit_reason"].apply(lambda s: (s == "trail").mean()),
    })
    out.index.name = "year"
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--trades-csv", type=Path, default=None)
    args = ap.parse_args()

    m1 = load_histdata_m1_utc(SYMBOL, DATA_DIR)
    m15 = resample_ohlc(m1, BAR_RULE)
    print(f"{SYMBOL} M1 (UTC): {m1.index.min()} .. {m1.index.max()} ({len(m1)} bars) "
          f"-> M15 {len(m15)} bars")

    base = trades_to_frame(simulate(m15, GOLD_CHANDELIER_BASE))
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
    print(f"  trail armed: {base['trail_armed'].sum()}/{len(base)} trades "
          f"({100 * base['trail_armed'].mean():.1f}%)")
    for d, g in base.groupby("direction"):
        print(f"  {d:5s}: n {len(g):4d}  win {100 * (g['r_multiple'] > 0).mean():5.1f}%  "
              f"mean R {g['r_multiple'].mean():+.4f}")
    for reason, g in base.groupby("exit_reason"):
        print(f"  exit={reason:5s}: n {len(g):4d}  mean R {g['r_multiple'].mean():+.3f}")

    print("\nWalk-forward by calendar year (base, 1bp):")
    print(by_year(base).to_string(float_format=lambda x: f"{x:9.3f}"))

    print("\nCost sensitivity (Gate 2) -- mean R per trade, overall and by year:")
    rows = {}
    for label, cfg in COST_SCENARIOS:
        tr = trades_to_frame(simulate(m15, cfg))
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
