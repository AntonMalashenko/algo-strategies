"""S007 b_reversal_to_A trigger: bare intrabar touch vs close-confirmed.

MOTIVATION (Anton, 2026-09-16, ALGODEV-42 follow-up): live, the GER40
recovery leg armed off an M1 bar whose low touched mid to the tenth of a
point (25478.9) and closed back at 25486.4 -- 7.5pt above, near its own
high -- the same minute. Anton's own chart read: "для сценария не было
закрепления за диапазоном 0.5" (no real hold/confirmation at the 0.5 level).
Verified against the engine (engine.py::simulate_day, ~line 296): the
reversal trigger is a bare `lows[t] <= mid` / `highs[t] >= mid` touch test on
ONE bar, filled at an assumed `mid` price -- unlike the day's PRIMARY A/B
detector (setups.py::find_setup), which requires a close beyond the level,
then the NEXT close still beyond it, before arming.

cfg.reversal_confirm_close (default False, base untouched) makes the
reversal trigger use that same two-close confirmation, filling at the
confirming bar's own close instead of an assumed mid fill. This script
tests whether that changes the b_reversal_to_A verdict already documented in
strategies/ger40_lonfra/config.py (REVERSAL_S007 / WORKING_S007 comments:
net +0.415->+0.531R at real spread, all years up; recovery leg alone
+91.5R/187 legs/70% win) -- that verdict was measured with the touch-only
rule, so it already includes whatever share of wick-only arms lost; this
checks whether requiring an actual confirmed close does better or worse.

Same gates as backtest/run_s007_breakeven.py: Gate 1 (gross) and Gate 2
(real spread REAL_SPREAD_PER_SIDE/side). Every recovery-leg-only breakdown
isolates `p["is_recovery"]` positions so the change (which only affects how/
whether a recovery leg is armed) is visible on its own, not diluted by the
unaffected primary leg.

Usage: python3 backtest/run_s007_reversal_confirm.py
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


def recovery_leg_stats(res: pd.DataFrame) -> dict:
    """Isolates is_recovery positions -- the only thing reversal_confirm_close
    can change -- so its effect doesn't get diluted by the unaffected primary
    leg. n_days is how many traded days actually armed a recovery leg at all
    (fewer under confirm=True is the expected direct effect of a stricter
    trigger; what matters is what happens to net_R and win_pct)."""
    n_days = n_pos = n_win = 0
    sum_R = 0.0
    for positions in res.get("positions", []):
        legs = [p for p in positions if p.get("is_recovery")]
        if legs:
            n_days += 1
        for p in legs:
            n_pos += 1
            sum_R += float(p["R"])
            if p["R"] > 0:
                n_win += 1
    return dict(n_days=n_days, n_pos=n_pos,
               win_pct=round(100 * n_win / n_pos, 1) if n_pos else float("nan"),
               sum_R=round(sum_R, 2),
               avg_R=round(sum_R / n_pos, 4) if n_pos else float("nan"))


def report(name, res, with_recovery: bool = False):
    n = len(res)
    if n == 0:
        print(f"{name:46s}  (no trades)")
        return None
    exp = res["day_R"].mean()
    dd = max_dd(res)
    winpct = 100 * (res["day_R"] > 0).mean()
    yrs = yearly(res)
    worst_year = min(yrs.values()) if yrs else float("nan")
    print(f"{name:46s}  n={n:4d}  net={exp:+.4f}R/day  sum={res['day_R'].sum():+7.1f}R  "
          f"days+={winpct:3.0f}%  maxDD={dd:7.1f}R  worst_yr={worst_year:+.1f}  years={yrs}")
    row = dict(name=name, n=n, net=round(float(exp), 4), sum=round(float(res["day_R"].sum()), 2),
               days_plus_pct=round(winpct, 1), maxDD=round(dd, 2), worst_year=worst_year, yrs=yrs)
    if with_recovery:
        rec = recovery_leg_stats(res)
        row.update(rec)
        print(f"{'':46s}   recovery legs: {rec['n_pos']:4d} positions on "
              f"{rec['n_days']:4d} days  win={rec['win_pct']:5.1f}%  "
              f"total={rec['sum_R']:+8.2f}R  avg={rec['avg_R']:+.4f}R")
    return row


def confirm_sweep(base_name, base_cfg, df, lv):
    print(f"\n=== {base_name}: reversal_confirm_close off vs on -- Gate 1 (gross) ===")
    for confirm in (False, True):
        cfg = base_cfg.with_(reversal_confirm_close=confirm)
        report(f"{base_name} confirm={confirm}", run(df, cfg, lv), with_recovery=True)

    print(f"\n=== {base_name}: reversal_confirm_close off vs on -- "
          f"Gate 2 (real spread {REAL_SPREAD_PER_SIDE}/side) ===")
    rows = []
    for confirm in (False, True):
        cfg = base_cfg.with_(reversal_confirm_close=confirm, spread_per_side=REAL_SPREAD_PER_SIDE)
        rows.append(report(f"{base_name} confirm={confirm} net", run(df, cfg, lv), with_recovery=True))
    return [r for r in rows if r is not None]


if __name__ == "__main__":
    df = D.load("duka")
    print(f"Dukascopy data: {df['date_only'].min()} .. {df['date_only'].max()} ({len(df)} bars)\n")
    lv = D.daily_levels(df)

    print("=== Regression check: base (confirm=False) must match documented numbers ===")
    report("BASELINE_S007 gross", run(df, C.BASELINE_S007, lv))
    report("REVERSAL_S007 gross", run(df, C.REVERSAL_S007, lv), with_recovery=True)
    report("WORKING_S007 gross", run(df, C.WORKING_S007, lv), with_recovery=True)
    report("WORKING_S007_NEWSSAFE_MAX8_BE05 gross",
           run(df, C.WORKING_S007_NEWSSAFE_MAX8_BE05, lv), with_recovery=True)

    # BASES: every config that actually enables b_reversal_to_A. REVERSAL_S007
    # is the frozen historical anchor (baseline + reversal only, no height
    # filter); WORKING_S007 is the recommended-champion chain's root; the live
    # deployment preset is the one the trading decision is actually about.
    BASES = [
        ("REVERSAL_S007", C.REVERSAL_S007),
        ("WORKING_S007", C.WORKING_S007),
        ("WORKING_S007_NEWSSAFE_MAX8_BE05 (live)", C.WORKING_S007_NEWSSAFE_MAX8_BE05),
    ]

    all_rows = {}
    for base_name, base_cfg in BASES:
        rows_net = confirm_sweep(base_name, base_cfg, df, lv)
        all_rows[base_name] = rows_net
        base_row = next((r for r in rows_net if "confirm=False" in r["name"]), None)
        confirm_row = next((r for r in rows_net if "confirm=True" in r["name"]), None)
        print(f"\n--- Summary ({base_name}, Gate 2 net) ---")
        for r in rows_net:
            delta = f"  d_net={r['net'] - base_row['net']:+.4f}" if base_row else ""
            print(f"  {r['name']:46s}  net={r['net']:+.4f}R/day{delta}  "
                  f"maxDD={r['maxDD']:7.1f}R  worst_yr={r['worst_year']:+.1f}  "
                  f"recovery: {r['n_pos']:4d} pos / {r['n_days']:4d} days  "
                  f"win={r['win_pct']:5.1f}%  total={r['sum_R']:+8.2f}R")
