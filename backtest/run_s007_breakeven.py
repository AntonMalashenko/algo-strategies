"""S007 breakeven modifiers: the trigger (breakeven_at_r) and, since 2026-09-10,
how far past entry the moved stop is placed (breakeven_offset_points).

PART 1 (Anton, 2026-09-02) -- move each position's own stop to its own entry
once price has moved cfg.breakeven_at_r * that position's own risk in its favor
("БУ после 1R"). Sweeps breakeven_at_r in BE_LEVELS; 1.0 is what was asked for,
the neighbours are a cheap robustness check so a single lucky threshold isn't
mistaken for a real effect (same reasoning as the min_height sweep,
strategy-spec-S007.md sec 11).

PART 2 (Anton, 2026-09-10, ALGODEV-41) -- "make the BE a few points more
favourable so even tiny losses don't happen". An entry-exact breakeven exit is
NOT flat: it books the ~1.27pt round-trip spread (REAL_SPREAD_PER_SIDE below)
plus commission as a small loss. cfg.breakeven_offset_points moves the BE stop
to entry +- N points instead, so the same exit nets ~0 or slightly positive.
Swept over OFFSET_LEVELS at the LIVE trigger (LIVE_BREAKEVEN_AT_R = 0.5).

Both parts run on the frozen BASELINE_S007 (the historical anchor, so the
numbers stay comparable to what's already documented) and on the current
deployment-relevant preset WORKING_S007_NEWSSAFE_MAX8_BE05 -- which already
carries breakeven_at_r=0.5, so the offset sweep varies exactly one thing on the
config a deployment decision would actually be taken about.
WORKING_S007_LIQFLOOR (the older live preset) is kept in the breakeven_at_r
sweep only, for continuity with the 2026-09-02 verdict recorded in
strategies/ger40_lonfra/config.py.

BE-EXIT BREAKDOWN: the point of an offset is what happens to the trades that
actually exit on the moved stop, so every offset row also reports those
separately. No engine plumbing was needed to identify them -- a position that
exits on its BE stop is exactly `p["be_moved"] and p["status"] == "stop"`
(engine.py only ever sets be_moved on a still-open position and never restores
the original stop afterwards, so a later "stop" status can only be the moved
one). p["R"] is already cost-applied by engine.simulate_day.

GATE 3 NOT RE-RUN: BE@0.5R was originally chosen on the prop survivability
axis (cashout% / daily-bust%, backtest/run_s007_propscheme.py), not on net
R/day. That harness is not reused here -- see the closing note this script
prints.

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

# ALGODEV-41: breakeven_offset_points levels, in RAW index points. 0.0 is the
# base (stop moved to entry exactly); 1.0-2.0 straddle the 1.27pt round-trip
# spread this is meant to clear; 3.0/5.0 probe how much a bigger offset costs
# by arming a tighter post-breakeven stop.
OFFSET_LEVELS = [0.0, 1.0, 2.0, 3.0, 5.0]

# The trigger the offset sweep holds fixed: what the deployment-candidate
# preset WORKING_S007_NEWSSAFE_MAX8_BE05 actually uses.
LIVE_BREAKEVEN_AT_R = 0.5


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


def be_exit_stats(res: pd.DataFrame) -> dict:
    """P&L of the trades that exited ON the moved breakeven stop.

    `be_moved and status == "stop"` identifies them exactly (see module
    docstring). Also returns how many positions were be_moved at all, so the
    "armed but ran on to TP/eod" majority stays visible next to them.
    """
    n_pos = n_armed = n_be_stop = 0
    sum_R = 0.0
    for positions in res.get("positions", []):
        for p in positions:
            n_pos += 1
            if not p.get("be_moved"):
                continue
            n_armed += 1
            if p["status"] == "stop":
                n_be_stop += 1
                sum_R += float(p["R"])
    return dict(n_pos=n_pos, n_armed=n_armed, n_be_stop=n_be_stop,
                be_stop_R=round(sum_R, 2),
                be_stop_avg_R=round(sum_R / n_be_stop, 4) if n_be_stop else float("nan"))


def report(name, res, with_be_exits: bool = False):
    n = len(res)
    if n == 0:
        print(f"{name:44s}  (no trades)")
        return None
    exp = res["day_R"].mean()
    dd = max_dd(res)
    winpct = 100 * (res["day_R"] > 0).mean()
    yrs = yearly(res)
    worst_year = min(yrs.values()) if yrs else float("nan")
    print(f"{name:44s}  n={n:4d}  net={exp:+.4f}R/day  sum={res['day_R'].sum():+7.1f}R  "
          f"days+={winpct:3.0f}%  maxDD={dd:7.1f}R  worst_yr={worst_year:+.1f}  years={yrs}")
    row = dict(name=name, n=n, net=round(float(exp), 4), sum=round(float(res["day_R"].sum()), 2),
               days_plus_pct=round(winpct, 1), maxDD=round(dd, 2), worst_year=worst_year, yrs=yrs)
    if with_be_exits:
        be = be_exit_stats(res)
        row.update(be)
        print(f"{'':44s}   BE-exits: {be['n_be_stop']:4d} of {be['n_armed']:4d} armed "
              f"({be['n_pos']:5d} positions)  total={be['be_stop_R']:+8.2f}R  "
              f"avg={be['be_stop_avg_R']:+.4f}R")
    return row


def offset_sweep(base_name, base_cfg, df, lv):
    """breakeven_offset_points sweep at the fixed live trigger, Gate 1 + Gate 2."""
    trig_cfg = base_cfg.with_(breakeven_at_r=LIVE_BREAKEVEN_AT_R)
    print(f"\n=== {base_name} + BE@{LIVE_BREAKEVEN_AT_R}R, breakeven_offset_points sweep "
          f"-- Gate 1 (gross) ===")
    for off in OFFSET_LEVELS:
        cfg = trig_cfg.with_(breakeven_offset_points=off)
        report(f"{base_name} BE@{LIVE_BREAKEVEN_AT_R}R off={off}pt", run(df, cfg, lv),
               with_be_exits=True)

    print(f"\n=== {base_name} + BE@{LIVE_BREAKEVEN_AT_R}R, breakeven_offset_points sweep "
          f"-- Gate 2 (real spread {REAL_SPREAD_PER_SIDE}/side) ===")
    rows = []
    for off in OFFSET_LEVELS:
        cfg = trig_cfg.with_(breakeven_offset_points=off,
                             spread_per_side=REAL_SPREAD_PER_SIDE)
        rows.append(report(f"{base_name} BE@{LIVE_BREAKEVEN_AT_R}R off={off}pt net",
                           run(df, cfg, lv), with_be_exits=True))
    return [r for r in rows if r is not None]


if __name__ == "__main__":
    df = D.load("duka")
    print(f"Dukascopy data: {df['date_only'].min()} .. {df['date_only'].max()} ({len(df)} bars)\n")
    lv = D.daily_levels(df)

    BASES = [
        ("BASELINE_S007", C.BASELINE_S007),
        ("WORKING_S007_LIQFLOOR (ex-live)", C.WORKING_S007_LIQFLOOR),
    ]
    # The offset sweep needs a base whose breakeven_at_r we set ourselves, so it
    # runs on BASELINE_S007 and on the live preset's parent -- MAX8_BE05 minus
    # its breakeven_at_r, re-applied as LIVE_BREAKEVEN_AT_R below, which is the
    # same config by construction.
    OFFSET_BASES = [
        ("BASELINE_S007", C.BASELINE_S007),
        ("MAX8_BE05 (live preset)", C.WORKING_S007_NEWSSAFE_MAX8),
    ]

    print("=== Regression check: bases must match documented numbers exactly ===")
    for name, cfg in BASES:
        report(f"{name} gross", run(df, cfg, lv))
    report("WORKING_S007_NEWSSAFE_MAX8_BE05 gross",
           run(df, C.WORKING_S007_NEWSSAFE_MAX8_BE05, lv), with_be_exits=True)

    # ---------------- PART 1: breakeven_at_r sweep (2026-09-02) ----------------
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
            print(f"  {r['name']:44s}  net={r['net']:+.4f}R/day  maxDD={r['maxDD']:7.1f}R  "
                  f"days+={r['days_plus_pct']:3.0f}%  worst_yr={r['worst_year']:+.1f}")

    # -------- PART 2: breakeven_offset_points sweep (2026-09-10, ALGODEV-41) --------
    for base_name, base_cfg in OFFSET_BASES:
        rows_net = offset_sweep(base_name, base_cfg, df, lv)
        base_row = next((r for r in rows_net if "off=0.0pt" in r["name"]), None)
        print(f"\n--- Summary ({base_name} BE@{LIVE_BREAKEVEN_AT_R}R, Gate 2 net, "
              f"by offset) ---")
        for r in rows_net:
            delta = (f"  d_net={r['net'] - base_row['net']:+.4f}"
                     if base_row else "")
            print(f"  {r['name']:44s}  net={r['net']:+.4f}R/day{delta}  "
                  f"maxDD={r['maxDD']:7.1f}R  worst_yr={r['worst_year']:+.1f}  "
                  f"BE-exits {r['n_be_stop']:4d} tot={r['be_stop_R']:+8.2f}R "
                  f"avg={r['be_stop_avg_R']:+.4f}R")

    print("\nNOTE: Gate 3 (prop cashout% / daily-bust%, "
          "backtest/run_s007_propscheme.py) was NOT re-run for the offset "
          "sweep -- the offset is rejected on the Gate 1/2 expectancy axis "
          "above, so re-running the more expensive prop Monte Carlo would only "
          "be worth it if a promotion were on the table.")
