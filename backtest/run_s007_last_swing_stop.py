"""S007 stop_mode="last_swing" vs the live "mid_range" stop, plus the new
last_swing_buffer_points modifier (2026-09-23, Anton's request).

WHAT'S CHANGING (stop only -- TP and everything else stays exactly as the live
preset WORKING_S007_NEWSSAFE_MAX8_BE05_OFF2, tp_mode="liquidity",
liquidity_tp_floor=True): stop_mode="mid_range" is ONE common stop shared by
every pyramided position in a leg (B -> mid 0.5, A -> opposite boundary).
stop_mode="last_swing" is per-position -- each position (the primary entry AND
every add) gets its OWN stop, behind the last confirmed structural swing at
the moment it opened. This mode already existed in pick_stop() but had never
been benchmarked against the live TP logic (tp_mode="liquidity") -- its only
prior use is REF_PYRAMID_DUKA, a byte-for-byte regression reproduction of an
old reference file that uses tp_mode="range" and exit at 11:59, not a
profitability read.

last_swing_buffer_points (new field, config.py) pushes the last_swing stop
this many RAW points further AWAY from entry (deeper, more risk) than the raw
swing extreme, same "make it a few points past the raw level" idea as
breakeven_offset_points -- so a wick that only retouches the swing low/high
without breaking structure doesn't tag the stop. Swept over BUFFER_LEVELS,
same methodology as the breakeven_offset_points sweep
(backtest/run_s007_breakeven.py): Gate 1 (gross) + Gate 2 (real spread
REAL_SPREAD_PER_SIDE), by year, plus an explicit n_stop comparison against the
mid_range baseline (the direct answer to "does the new stop get hit more or
less often than the shared one").

Usage: python3 backtest/run_s007_last_swing_stop.py
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

# last_swing_buffer_points levels: 0.0 is the raw swing-extreme stop; 1-3 probe
# a small wick buffer; 5 checks how much a bigger buffer costs by widening
# every position's risk (and therefore its dollar risk at fixed % sizing).
BUFFER_LEVELS = [0.0, 1.0, 2.0, 3.0, 5.0]

# The live preset as of 2026-09-23 (webapp DB account_strategies.preset for
# account_strategy id=1, per .claude/change-log/ger40_lonfra.jsonl
# 2026-09-11). Everything except stop_mode/last_swing_buffer_points is held
# fixed to this exactly -- TP, breakeven, offset, max_positions, NEWSSAFE
# early close all stay as-is.
LIVE_BASE = C.WORKING_S007_NEWSSAFE_MAX8_BE05_OFF2


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


def worst_day(res: pd.DataFrame) -> float:
    if len(res) == 0:
        return float("nan")
    return float(res["day_R"].min())


def report(name, res):
    n = len(res)
    if n == 0:
        print(f"{name:56s}  (no trades)")
        return None
    exp = res["day_R"].mean()
    dd = max_dd(res)
    winpct = 100 * (res["day_R"] > 0).mean()
    yrs = yearly(res)
    worst_year = min(yrs.values()) if yrs else float("nan")
    wday = worst_day(res)
    n_pos = int(res["n_pos"].sum())
    n_stop = int(res["n_stop"].sum())
    n_tp = int(res["n_tp"].sum())
    n_eod = int(res["n_eod"].sum())
    stop_pct = 100 * n_stop / n_pos if n_pos else float("nan")
    print(f"{name:56s}  n={n:4d}  net={exp:+.4f}R/day  sum={res['day_R'].sum():+8.1f}R  "
          f"days+={winpct:3.0f}%  maxDD={dd:7.1f}R  worst_day={wday:+6.2f}R  worst_yr={worst_year:+7.1f}")
    print(f"{'':56s}   positions={n_pos:5d}  stop={n_stop:5d} ({stop_pct:4.1f}%)  "
          f"tp={n_tp:5d}  eod={n_eod:5d}")
    return dict(name=name, n=n, net=round(float(exp), 4), sum=round(float(res["day_R"].sum()), 2),
                days_plus_pct=round(winpct, 1), maxDD=round(dd, 2), worst_day=round(wday, 2),
                worst_year=worst_year, yrs=yrs, n_pos=n_pos, n_stop=n_stop, n_tp=n_tp,
                n_eod=n_eod, stop_pct=round(stop_pct, 2))


def buffer_sweep(base_name, base_cfg, df, lv, gate2):
    """last_swing stop, buffer sweep, at Gate 1 (gate2=False) or Gate 2 (gate2=True)."""
    gate_cfg = base_cfg.with_(spread_per_side=REAL_SPREAD_PER_SIDE) if gate2 else base_cfg
    tag = f"Gate 2 (real spread {REAL_SPREAD_PER_SIDE}/side)" if gate2 else "Gate 1 (gross)"
    print(f"\n=== {base_name} + stop_mode=last_swing, buffer sweep -- {tag} ===")
    rows = []
    for buf in BUFFER_LEVELS:
        cfg = gate_cfg.with_(stop_mode="last_swing", last_swing_buffer_points=buf)
        rows.append(report(f"{base_name} last_swing buf={buf}pt" + (" net" if gate2 else ""),
                           run(df, cfg, lv)))
    return [r for r in rows if r is not None]


if __name__ == "__main__":
    df = D.load("duka")
    print(f"Dukascopy data: {df['date_only'].min()} .. {df['date_only'].max()} ({len(df)} bars)\n")
    lv = D.daily_levels(df)

    print("=== Regression sanity: existing presets unaffected by this change ===")
    report("BASELINE_S007 gross", run(df, C.BASELINE_S007, lv))
    report("LIVE_BASE (WORKING_S007_NEWSSAFE_MAX8_BE05_OFF2) gross", run(df, LIVE_BASE, lv))

    # ---------------- Gate 1 (gross): mid_range baseline vs last_swing sweep ----------------
    print(f"\n=== LIVE_BASE, stop_mode=mid_range (unchanged, as-is) -- Gate 1 (gross) ===")
    mid_range_gross = report("LIVE_BASE mid_range (current live stop)", run(df, LIVE_BASE, lv))
    gross_rows = buffer_sweep("LIVE_BASE", LIVE_BASE, df, lv, gate2=False)

    # ---------------- Gate 2 (real spread): the one the decision is made on ----------------
    print(f"\n=== LIVE_BASE, stop_mode=mid_range (unchanged, as-is) -- "
          f"Gate 2 (real spread {REAL_SPREAD_PER_SIDE}/side) ===")
    mid_range_net = report("LIVE_BASE mid_range net (current live stop)",
                           run(df, LIVE_BASE.with_(spread_per_side=REAL_SPREAD_PER_SIDE), lv))
    net_rows = buffer_sweep("LIVE_BASE", LIVE_BASE, df, lv, gate2=True)

    print(f"\n--- Summary (Gate 2 net, sorted by net R/day) ---")
    all_net = ([mid_range_net] if mid_range_net else []) + net_rows
    for r in sorted(all_net, key=lambda r: -r["net"]):
        d_net = r["net"] - mid_range_net["net"] if mid_range_net else float("nan")
        d_stop = r["n_stop"] - mid_range_net["n_stop"] if mid_range_net else 0
        print(f"  {r['name']:56s}  net={r['net']:+.4f}R/day  d_net={d_net:+.4f}  "
              f"maxDD={r['maxDD']:7.1f}R  worst_day={r['worst_day']:+6.2f}R  "
              f"worst_yr={r['worst_year']:+7.1f}  stop={r['n_stop']:5d} "
              f"({r['stop_pct']:4.1f}%)  d_stop={d_stop:+5d}  days+={r['days_plus_pct']:3.0f}%")

    print(f"\n--- By year (Gate 2 net) ---")
    for r in all_net:
        print(f"  {r['name']:56s}  {r['yrs']}")

    print("\nNOTE: Gate 3 (prop cashout% / daily-bust%, backtest/run_s007_propscheme.py) "
          "not re-run here -- only worth it if a buffer value beats mid_range on the "
          "Gate 1/2 expectancy axis above and a promotion is actually on the table.")
