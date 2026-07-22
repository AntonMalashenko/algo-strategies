"""Backtest harness for S007 filter/risk-model experiments (2026-07-22).

Uses the real Dukascopy GER40 M1 data (re-fetched 2026-07-22 via dukascopy-node
after the original file was found to be lost -- see decisions-log.md and
strategy-spec-S007.md sec 12/14) through strategies.ger40_lonfra.data.load('duka').
Does not modify data.py or the engine -- reuses config.py presets unchanged, so
results are directly comparable to strategy-spec-S007.md.

Usage: python3 backtest/run_s007_filters.py
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
        print(f"{name:55s}  (no trades)")
        return None
    exp = res["day_R"].mean()
    dd = max_dd(res)
    print(f"{name:55s}  n={n:4d}  net={exp:+.3f}R/day  maxDD={dd:7.1f}R  years={yearly(res)}")
    return dict(name=name, n=n, net=round(float(exp), 4), maxDD=round(dd, 2), years=yearly(res))


if __name__ == "__main__":
    df = D.load("duka")
    print(f"Dukascopy data: {df['date_only'].min()} .. {df['date_only'].max()} ({len(df)} bars)\n")
    lv = D.daily_levels(df)

    baseline_net = C.BASELINE_S007.with_(spread_per_side=REAL_SPREAD_PER_SIDE)
    working_net = C.WORKING_S007.with_(spread_per_side=REAL_SPREAD_PER_SIDE)

    print("=== Regression sanity (doc: BASELINE +0.415R, WORKING +0.571R) ===")
    report("BASELINE_S007 net", run(df, baseline_net, lv))
    report("WORKING_S007 net", run(df, working_net, lv))

    print("\n=== E: min_height sweep (on top of BASELINE net) ===")
    for mh in [0, 5, 10, 15, 20, 25, 30, 40]:
        cfg = baseline_net.with_(min_height=(mh if mh > 0 else None))
        report(f"min_height={mh}", run(df, cfg, lv))

    print("\n=== E: min_height sweep (on top of WORKING net) ===")
    for mh in [0, 5, 10, 15, 20, 25, 30, 40]:
        cfg = working_net.with_(min_height=(mh if mh > 0 else None))
        report(f"WORKING + min_height={mh}", run(df, cfg, lv))

    print("\n=== E: unlimited_adds + daily_loss_cap_R sweep (on top of BASELINE net) ===")
    report("max_positions=4 (current model, sanity)", run(df, baseline_net, lv))
    for cap in [4, 6, 8, 10, 12, 16]:
        cfg = baseline_net.with_(unlimited_adds=True, daily_loss_cap_R=cap)
        report(f"daily_loss_cap_R={cap}", run(df, cfg, lv))
