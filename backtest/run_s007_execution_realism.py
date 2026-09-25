"""S007: how much of the backtest's expectancy is executable at 1-minute
polling, vs. a hindsight-only artifact of full-bar-close replay?

MOTIVATION (Anton, 2026-09-16, ALGODEV-44 follow-up): a direct live-vs-model
parity check on 5 real trading days (2026-09-10/11/14/15/16) found the model
(engine.simulate_day, replayed on the SAME real broker bars) would have
netted about +$88.87 over the week, while the live bot actually netted
-$196.02 -- a ~$285 gap. Digging into 2026-09-16 specifically: the model
found 7 positions in the PRIMARY leg alone (vs. 1 placed live) -- price
chopped back and forth across the shared mid-range stop repeatedly, and each
chop registers to the full-hindsight replay as "entered, then stopped" (often
at a POSITIVE R, via the already-documented ALGODEV-40 backward-stop
mechanic: an add's own entry can end up numerically past the leg's shared,
distant stop by the time it's placed). A live bot polling once a minute can
only ever place an order for a position from the FIRST cycle whose last
CLOSED bar equals that position's own entry bar (bot/s007_paper.py::decide
places a newly "eod" position the same cycle it's first detected) -- which
is structurally already >=1 minute of real time after that bar's close (see
run_live_cycle's KNOWN GAP comment, bot/ctrader_s007.py). If the SAME
position's exit already happened within that entry bar or the very next one,
no live bot -- however fast its own code runs -- could ever have caught it.
This is a different, larger effect than ALGODEV-44's per-cycle latency
question: even a hypothetical zero-latency bot cannot execute a round trip
that completes inside a single M1 bar.

engine.py's `exit_idx` (added alongside this script, 2026-09-16, purely
additive -- feeds no R/cost calc, proven byte-identical via the regression
check below) makes this filterable: MIN_CATCHABLE_BARS below is the minimum
(exit_idx - idx) a position needs to have been realistically live on the
broker's book for any meaningful stretch. Swept at 2 and 3 bars as a
robustness check, same reasoning as every other threshold sweep in this
strategy's backtests -- a single arbitrary cutoff proves nothing on its own.

Usage: python3 backtest/run_s007_execution_realism.py
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

REAL_SPREAD_PER_SIDE = 0.635  # Gate 2, strategy-spec-S007.md sec 10.2

# How many bars a position must have stayed open (exit_idx - idx) to count as
# realistically catchable by a 1-minute-polling live bot. 1 is already
# provably impossible (same-bar or next-bar round trip); 2-3 is the
# sensitivity band this script sweeps.
CATCHABLE_THRESHOLDS = [2, 3]


def max_dd(day_R: pd.Series, dates: pd.Series) -> float:
    if len(day_R) == 0:
        return 0.0
    order = dates.argsort()
    cum = day_R.to_numpy()[order].cumsum()
    return float((cum - np.maximum.accumulate(cum)).min())


def yearly(day_R: pd.Series, dates: pd.Series) -> dict:
    if len(day_R) == 0:
        return {}
    yrs = pd.to_datetime(dates).dt.year
    return {int(y): round(float(day_R[yrs == y].sum()), 2) for y in sorted(yrs.unique())}


def realistic_report(name: str, res: pd.DataFrame, cfg: C.StrategyConfig, min_bars: int):
    """Recompute each day's day_R using ONLY positions realistically
    executable at 1-minute polling (exit_idx - idx >= min_bars, weighted by
    cfg.risk_per_add same as engine.simulate_day itself); an unexecutable
    ("ghost") position contributes 0, matching what a live bot actually
    experiences (bot/s007_signals.py::plan_now surfaces these separately as
    `resolved`/ghost trades -- they never reach the broker, so they can never
    move real P&L)."""
    rows = []
    for _, row in res.iterrows():
        real_R = ghost_R = 0.0
        n_ghost = n_total = 0
        for p in row["positions"]:
            n_total += 1
            weight = cfg.risk_per_add if p["is_add"] else 1.0
            exit_idx = p.get("exit_idx")
            catchable = exit_idx is not None and (exit_idx - p["idx"]) >= min_bars
            if catchable:
                real_R += weight * p["R"]
            else:
                n_ghost += 1
                ghost_R += weight * p["R"]
        rows.append(dict(date=row["date"], day_R=real_R, ghost_R=ghost_R,
                         n_ghost=n_ghost, n_total=n_total))
    real = pd.DataFrame(rows)
    n = len(real)
    if n == 0:
        print(f"{name:48s}  (no trades)")
        return None
    exp = real["day_R"].mean()
    dd = max_dd(real["day_R"], real["date"])
    winpct = 100 * (real["day_R"] > 0).mean()
    yrs = yearly(real["day_R"], real["date"])
    worst_year = min(yrs.values()) if yrs else float("nan")
    total_pos = real["n_total"].sum()
    total_ghost = real["n_ghost"].sum()
    ghost_R_sum = real["ghost_R"].sum()
    print(f"{name:48s}  n={n:4d}  net={exp:+.4f}R/day  sum={real['day_R'].sum():+7.1f}R  "
          f"days+={winpct:3.0f}%  maxDD={dd:7.1f}R  worst_yr={worst_year:+.1f}")
    print(f"{'':48s}   ghosts: {total_ghost:5d} of {total_pos:5d} positions "
          f"({100*total_ghost/total_pos:.1f}%)  excluded_R={ghost_R_sum:+8.1f}R "
          f"(what hindsight-only replay counted that live never could have)")
    return dict(name=name, net=round(float(exp), 4), sum=round(float(real["day_R"].sum()), 2),
               maxDD=round(dd, 2), worst_year=worst_year,
               ghost_pct=round(100 * total_ghost / total_pos, 1), ghost_R=round(float(ghost_R_sum), 1))


def full_hindsight_report(name: str, res: pd.DataFrame):
    n = len(res)
    if n == 0:
        print(f"{name:48s}  (no trades)")
        return None
    exp = res["day_R"].mean()
    dd = max_dd(res["day_R"], pd.to_datetime(res["date"]))
    winpct = 100 * (res["day_R"] > 0).mean()
    yrs = yearly(res["day_R"], pd.to_datetime(res["date"]))
    worst_year = min(yrs.values()) if yrs else float("nan")
    print(f"{name:48s}  n={n:4d}  net={exp:+.4f}R/day  sum={res['day_R'].sum():+7.1f}R  "
          f"days+={winpct:3.0f}%  maxDD={dd:7.1f}R  worst_yr={worst_year:+.1f}")
    return dict(name=name, net=round(float(exp), 4), sum=round(float(res["day_R"].sum()), 2),
               maxDD=round(dd, 2), worst_year=worst_year)


if __name__ == "__main__":
    df = D.load("duka")
    print(f"Dukascopy data: {df['date_only'].min()} .. {df['date_only'].max()} ({len(df)} bars)\n")
    lv = D.daily_levels(df)

    print("=== Regression check: gross full-hindsight numbers must match documented ones ===")
    full_hindsight_report("BASELINE_S007 gross", run(df, C.BASELINE_S007, lv))
    full_hindsight_report("WORKING_S007_NEWSSAFE_MAX8_BE05 gross",
                          run(df, C.WORKING_S007_NEWSSAFE_MAX8_BE05, lv))

    BASES = [
        ("BASELINE_S007", C.BASELINE_S007),
        ("REVERSAL_S007", C.REVERSAL_S007),
        ("WORKING_S007", C.WORKING_S007),
        ("WORKING_S007_NEWSSAFE_MAX8_BE05 (live)", C.WORKING_S007_NEWSSAFE_MAX8_BE05),
    ]

    for base_name, base_cfg in BASES:
        cfg_net = base_cfg.with_(spread_per_side=REAL_SPREAD_PER_SIDE)
        res = run(df, cfg_net, lv)
        print(f"\n=== {base_name} -- Gate 2 (real spread {REAL_SPREAD_PER_SIDE}/side) ===")
        full_row = full_hindsight_report(f"{base_name} FULL HINDSIGHT (as validated)", res)
        for min_bars in CATCHABLE_THRESHOLDS:
            r = realistic_report(f"{base_name} REALISTIC (>={min_bars} bars held)",
                                 res, cfg_net, min_bars)
            if r and full_row:
                gap = full_row["net"] - r["net"]
                print(f"{'':48s}   gap vs full hindsight: {gap:+.4f}R/day "
                      f"({100*gap/full_row['net']:+.1f}% of the validated number)")
