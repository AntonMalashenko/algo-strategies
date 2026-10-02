"""Diagnostic (2026-10-02): how much of S007's backtest comes from adds whose shared stop was already
on the WRONG side of their own entry (a short with stop below entry / a long
with stop above) -- trades the live bot can never open (broker rejects them,
and since 2026-10-02 decide() skips them). Read-only: uses the engine as-is and
only re-aggregates day_R without those positions. Gross and Gate-2 net.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pandas as pd
from strategies.ger40_lonfra import config as C
from strategies.ger40_lonfra import data as D
from strategies.ger40_lonfra.engine import run

REAL_SPREAD_PER_SIDE = 0.635   # same Gate-2 net cost as backtest/run_s007_breakeven.py
PRESET = C.WORKING_S007_NEWSSAFE_MAX8_BE05_OFF2


def wrong_side(p):
    stop0 = p.get("stop0", p["stop"])
    return (p["up"] and stop0 >= p["entry"]) or ((not p["up"]) and stop0 <= p["entry"])


def summarize(res, cfg, label):
    w_add = cfg.risk_per_add
    days = []
    for _, r in res.iterrows():
        pos = r["positions"]
        bad = [p for p in pos if wrong_side(p)]
        bad_R = sum((w_add if p["is_add"] else 1.0) * p["R"] for p in bad)
        days.append(dict(date=r["date"], day_R=r["day_R"], clean_R=r["day_R"] - bad_R,
                         n_bad=len(bad), bad_R=bad_R,
                         n_bad_add=sum(1 for p in bad if p["is_add"]),
                         n_bad_rec=sum(1 for p in bad if p.get("is_recovery"))))
    d = pd.DataFrame(days)
    d["year"] = pd.to_datetime(d["date"]).dt.year

    def stats(col):
        eq = d[col].cumsum()
        dd = (eq - eq.cummax()).min()
        wins = (d[col] > 0).mean()
        return d[col].sum(), d[col].mean(), dd, wins

    tot, avg, dd, wr = stats("day_R")
    ctot, cavg, cdd, cwr = stats("clean_R")
    print(f"\n=== {label} ({len(d)} traded days, {d['date'].min()} .. {d['date'].max()}) ===")
    print(f"wrong-side positions: {int(d['n_bad'].sum())} on {int((d['n_bad'] > 0).sum())} days "
          f"(adds {int(d['n_bad_add'].sum())}, in recovery legs {int(d['n_bad_rec'].sum())}); "
          f"their total R = {d['bad_R'].sum():+.1f}")
    print(f"{'':12s}{'total R':>10s}{'R/day':>9s}{'maxDD R':>10s}{'win days':>10s}")
    print(f"{'as now':12s}{tot:10.1f}{avg:9.3f}{dd:10.1f}{wr:10.1%}")
    print(f"{'without':12s}{ctot:10.1f}{cavg:9.3f}{cdd:10.1f}{cwr:10.1%}")
    print(f"share of total R from wrong-side positions: {(tot - ctot) / tot:.1%}" if tot else "")
    y = d.groupby("year")[["day_R", "clean_R", "bad_R"]].sum().round(1)
    print(y.to_string())



if __name__ == "__main__":
    df = D.load("duka")
    lv = D.daily_levels(df)
    summarize(run(df, PRESET, lv), PRESET, "WORKING_S007_NEWSSAFE_MAX8_BE05_OFF2 gross")
    net = PRESET.with_(spread_per_side=REAL_SPREAD_PER_SIDE)
    summarize(run(df, net, lv), net, "net (Gate-2 cost)")
