"""Gate 1 (gross) / Gate 2 (real-spread walk-forward) validation for ALGODEV-21:
liquidity_tp_floor (WORKING_S007_LIQFLOOR vs WORKING_S007), on the 2026-08-11
Dukascopy GER40 M1 refresh (2023-06-26 .. 2026-08-11).

Does not modify engine.py/config.py/data.py logic -- reuses the presets and the
consolidated engine unchanged, so results are directly comparable to
backtest/run_s007_filters.py's regression numbers.

Usage: python3 backtest/run_s007_liqfloor.py
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
        print(f"{name:45s}  (no trades)")
        return None
    exp = res["day_R"].mean()
    dd = max_dd(res)
    winpct = 100 * (res["day_R"] > 0).mean()
    print(f"{name:45s}  n={n:4d}  net={exp:+.4f}R/day  sum={res['day_R'].sum():+7.1f}R  "
          f"days+={winpct:3.0f}%  maxDD={dd:7.1f}R  years={yearly(res)}")
    return dict(name=name, n=n, net=round(float(exp), 4), sum=round(float(res["day_R"].sum()), 2),
                maxDD=round(dd, 2), years=yearly(res))


if __name__ == "__main__":
    df = D.load("duka")
    print(f"Dukascopy data: {df['date_only'].min()} .. {df['date_only'].max()} ({len(df)} bars)\n")
    lv = D.daily_levels(df)

    print("=== Regression sanity vs 2026-07-22 refresh (doc: BASELINE net +0.437R, WORKING net +0.536R) ===")
    baseline_net = C.BASELINE_S007.with_(spread_per_side=REAL_SPREAD_PER_SIDE)
    working_net = C.WORKING_S007.with_(spread_per_side=REAL_SPREAD_PER_SIDE)
    report("BASELINE_S007 net", run(df, baseline_net, lv))
    report("WORKING_S007 net", run(df, working_net, lv))

    print("\n=== Gate 1 (gross, no costs) -- WORKING_S007 vs WORKING_S007_LIQFLOOR ===")
    report("WORKING_S007 gross", run(df, C.WORKING_S007, lv))
    report("WORKING_S007_LIQFLOOR gross", run(df, C.WORKING_S007_LIQFLOOR, lv))

    print("\n=== Gate 2 (real spread 0.635/side) -- WORKING_S007 vs WORKING_S007_LIQFLOOR ===")
    liqfloor_net = C.WORKING_S007_LIQFLOOR.with_(spread_per_side=REAL_SPREAD_PER_SIDE)
    report("WORKING_S007 net", run(df, working_net, lv))
    report("WORKING_S007_LIQFLOOR net", run(df, liqfloor_net, lv))

    print("\n=== Same, but only days where tp_mode=liquidity picked a NEAR target (diagnostic) ===")
    # Re-run WORKING_S007 (no floor) and flag days whose realized tp sat closer to
    # entry than range_tp -- exactly the ALGODEV-21 pattern -- to see how many days
    # and how much R the floor actually touches.
    res_old = run(df, working_net, lv)
    if len(res_old):
        range_tp = res_old.apply(
            lambda r: (r["rh"] + r["height"]) if r["direction"] == "up" else (r["rl"] - r["height"]), axis=1)
        near = res_old[(res_old["direction"] == "up") & (res_old["tp"] < range_tp)
                       | (res_old["direction"] == "down") & (res_old["tp"] > range_tp)]
        print(f"days where liquidity tp was NEARER than range_tp: {len(near)} / {len(res_old)} "
              f"({100*len(near)/len(res_old):.1f}%)")
        print(f"  their combined day_R (old/no-floor): {near['day_R'].sum():+.2f}R "
              f"(avg {near['day_R'].mean():+.4f}R/day)")
