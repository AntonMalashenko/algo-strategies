"""Gate 1 (gross) / Gate 2 (real-spread) sweep for ALGODEV-35: does capping
how far a liquidity TP may sit BEYOND range_tp help or hurt, on the same
2026-08-11 Dukascopy GER40 M1 refresh (2023-06-26 .. 2026-08-11)
backtest/run_s007_liqfloor.py used for the ALGODEV-21 floor validation.

liquidity_tp_floor (already live) stops a target from being NEARER than
range_tp. liquidity_tp_ceiling_points (new, untested) stops it from being
FARTHER than range_tp + N points -- found worth checking live 2026-09-02
(Anton): a real cycle picked liquidity 285.9pt beyond range_tp, a move
unlikely to complete inside one session (S007 flattens at exit_end
regardless, so an unreached target usually just resolves 'eod').

Sweeps N = 10/20/30/40/50/100 against the uncapped WORKING_S007_LIQFLOOR
baseline. Does not modify engine.py/config.py logic beyond what ALGODEV-35
already added (liquidity_tp_ceiling_points, default None = unchanged
behavior) -- WORKING_S007_LIQFLOOR itself is untouched, so its numbers here
must match backtest/run_s007_liqfloor.py's exactly (regression check below).

Usage: python3 backtest/run_s007_liqceiling.py
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

CEILINGS = [
    ("WORKING_S007_LIQFLOOR (uncapped)", C.WORKING_S007_LIQFLOOR),
    ("LIQCEIL10", C.WORKING_S007_LIQCEIL10),
    ("LIQCEIL20", C.WORKING_S007_LIQCEIL20),
    ("LIQCEIL30", C.WORKING_S007_LIQCEIL30),
    ("LIQCEIL40", C.WORKING_S007_LIQCEIL40),
    ("LIQCEIL50", C.WORKING_S007_LIQCEIL50),
    ("LIQCEIL100", C.WORKING_S007_LIQCEIL100),
]


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
        print(f"{name:35s}  (no trades)")
        return None
    exp = res["day_R"].mean()
    dd = max_dd(res)
    winpct = 100 * (res["day_R"] > 0).mean()
    print(f"{name:35s}  n={n:4d}  net={exp:+.4f}R/day  sum={res['day_R'].sum():+7.1f}R  "
          f"days+={winpct:3.0f}%  maxDD={dd:7.1f}R  years={yearly(res)}")
    return dict(name=name, n=n, net=round(float(exp), 4), sum=round(float(res["day_R"].sum()), 2),
               days_plus_pct=round(winpct, 1), maxDD=round(dd, 2), years=yearly(res))


if __name__ == "__main__":
    df = D.load("duka")
    print(f"Dukascopy data: {df['date_only'].min()} .. {df['date_only'].max()} ({len(df)} bars)\n")
    lv = D.daily_levels(df)

    print("=== Regression check: WORKING_S007_LIQFLOOR must match run_s007_liqfloor.py exactly ===")
    report("WORKING_S007_LIQFLOOR gross", run(df, C.WORKING_S007_LIQFLOOR, lv))

    print("\n=== Gate 1 (gross, no costs) -- ceiling sweep ===")
    rows_gross = []
    for name, cfg in CEILINGS:
        rows_gross.append(report(name, run(df, cfg, lv)))

    print("\n=== Gate 2 (real spread 0.635/side) -- ceiling sweep ===")
    rows_net = []
    for name, cfg in CEILINGS:
        cfg_net = cfg.with_(spread_per_side=REAL_SPREAD_PER_SIDE)
        rows_net.append(report(f"{name} net", run(df, cfg_net, lv)))

    print("\n=== Summary (Gate 2 net, sorted by net R/day) ===")
    valid = [r for r in rows_net if r is not None]
    for r in sorted(valid, key=lambda r: -r["net"]):
        print(f"  {r['name']:35s}  net={r['net']:+.4f}R/day  maxDD={r['maxDD']:7.1f}R  "
              f"days+={r['days_plus_pct']:3.0f}%  worst_year={min(r['years'].values()) if r['years'] else 'n/a'}")
