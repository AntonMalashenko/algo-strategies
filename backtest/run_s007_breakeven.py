"""Modifier test (Anton, 2026-09-02): move each position's own stop to its own
entry (breakeven) once price has moved cfg.breakeven_at_r * that position's own
risk in its favor -- "БУ после 1R". New config field breakeven_at_r (default
None = off, base untouched) and the mechanic itself live in
strategies/ger40_lonfra/{config,engine}.py.

Stacked on top of the CURRENT LIVE preset (WORKING_S007_LIQFLOOR,
bot/s007_config.py::PRESET), not just the frozen BASELINE_S007, since that's
the config any verdict here should actually be compared against for a
deployment decision. BASELINE_S007 included alongside for reference against
the historically-documented numbers.

Sweeps breakeven_at_r in {0.5, 1.0, 1.5, 2.0} -- 1.0 is what was asked for
("после 1R"); the neighbors are a cheap robustness check so a single lucky
threshold isn't mistaken for a real effect (same reasoning as the min_height
sweep, strategy-spec-S007.md sec 11).

Usage: python3 backtest/run_s007_breakeven.py
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from strategies.ger40_lonfra import config as C
from strategies.ger40_lonfra import data as D
from strategies.ger40_lonfra.engine import run

REAL_SPREAD_PER_SIDE = 0.635  # Gate 2, strategy-spec-S007.md sec 10.2: 1.27pt round-trip / 2

BE_LEVELS = [0.5, 1.0, 1.5, 2.0]


def max_dd(res: pd.DataFrame) -> float:
    if len(res) == 0:
        return 0.0
    cum = res.sort_values("date")["day_R"].cumsum().to_numpy()
    return float((cum - np.maximum.accumulate(cum)).min())


def yearly(res: pd.DataFrame) -> dict:
    if len(res) == 0:
        return {}
    yrs = pd.to_datetime(res["date"]).dt.year
    return {int(y): round(float(sub["day_R"].sum()), 2) for y, sub in res.groupby(yrs)}


def report(name, res):
    n = len(res)
    if n == 0:
        print(f"{name:40s}  (no trades)")
        return None
    exp = res["day_R"].mean()
    dd = max_dd(res)
    winpct = 100 * (res["day_R"] > 0).mean()
    yrs = yearly(res)
    worst_year = min(yrs.values()) if yrs else float("nan")
    print(f"{name:40s}  n={n:4d}  net={exp:+.4f}R/day  sum={res['day_R'].sum():+7.1f}R  "
          f"days+={winpct:3.0f}%  maxDD={dd:7.1f}R  worst_yr={worst_year:+.1f}  years={yrs}")
    return dict(name=name, n=n, net=round(float(exp), 4), sum=round(float(res["day_R"].sum()), 2),
                days_plus_pct=round(winpct, 1), maxDD=round(dd, 2), worst_year=worst_year, years=yrs)


if __name__ == "__main__":
    df = D.load("duka")
    print(f"Dukascopy data: {df['date_only'].min()} .. {df['date_only'].max()} ({len(df)} bars)\n")
    lv = D.daily_levels(df)

    BASES = [
        ("BASELINE_S007", C.BASELINE_S007),
        ("WORKING_S007_LIQFLOOR (live preset)", C.WORKING_S007_LIQFLOOR),
    ]

    print("=== Regression check: bases must match documented numbers exactly ===")
    for name, cfg in BASES:
        report(f"{name} gross", run(df, cfg, lv))

    for base_name, base_cfg in BASES:
        print(f"\n=== {base_name} + breakeven_at_r sweep -- Gate 1 (gross) ===")
        rows_gross = [report(f"{base_name} (BE off)", run(df, base_cfg, lv))]
        for r in BE_LEVELS:
            cfg = base_cfg.with_(breakeven_at_r=r)
            rows_gross.append(report(f"{base_name} + BE@{r}R", run(df, cfg, lv)))

        print(f"\n=== {base_name} + breakeven_at_r sweep -- Gate 2 (real spread {REAL_SPREAD_PER_SIDE}/side) ===")
        rows_net = [report(f"{base_name} (BE off) net", run(df, base_cfg.with_(spread_per_side=REAL_SPREAD_PER_SIDE), lv))]
        for r in BE_LEVELS:
            cfg = base_cfg.with_(breakeven_at_r=r, spread_per_side=REAL_SPREAD_PER_SIDE)
            rows_net.append(report(f"{base_name} + BE@{r}R net", run(df, cfg, lv)))

        print(f"\n--- Summary ({base_name}, Gate 2 net, sorted by net R/day) ---")
        valid = [r for r in rows_net if r is not None]
        for r in sorted(valid, key=lambda r: -r["net"]):
            print(f"  {r['name']:40s}  net={r['net']:+.4f}R/day  maxDD={r['maxDD']:7.1f}R  "
                  f"days+={r['days_plus_pct']:3.0f}%  worst_yr={r['worst_year']:+.1f}")
