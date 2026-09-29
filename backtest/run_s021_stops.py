"""S021 stop variants on the BASE ORB path: size sweep, no stop, trailing, close-based, time stop.

Requested by Anton, 2026-09-28: "протестируй разные варианты стопа на базовой версии".
Everything except the stop is ORB_BASE (k=0.20, entry at the band, 15:59 time exit, 1 trade/day).

R normalisation: each variant's R = net_pts / its OWN initial risk (stop_adr_mult * ADR14) --
that is what a fixed-%-risk position sizer does, so R and the Gate 3 prop numbers are directly
comparable across stop sizes. For NO STOP (stop_adr_mult=inf) a nominal 0.75*ADR14 sizing risk
is used, and a single trade can then lose more than 1R. Close-based stops can also exceed -1R.

Usage: python3 backtest/run_s021_stops.py
"""
from __future__ import annotations

import math
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from strategies.orb_intraday.config import ORB_BASE, OrbConfig
from strategies.orb_intraday.engine import (
    load_nsxusd_m1, compute_daily_sessions, compute_adr14, simulate, trades_to_frame,
)
from backtest.run_s021_propscheme import prop_sim, DATA_DIR, SEED as SOLO_SEED

REGRESSION_N = 1518
REGRESSION_SUM_NET_PTS = 16220.614
NOMINAL_RISK_ADR_MULT = 0.75           # sizing risk for the no-stop variant (= base stop)
COST_STRESS_BPS = (1.0, 3.0)
SOLO_RISK_PCT = 0.50                   # live S021 risk setting
SPLIT_YEAR = 2023

VARIANTS: list[tuple[str, OrbConfig]] = [
    ("size 0.20 (=open O)", ORB_BASE.with_(stop_adr_mult=0.20)),
    ("size 0.30", ORB_BASE.with_(stop_adr_mult=0.30)),
    ("size 0.40 (=opp band)", ORB_BASE.with_(stop_adr_mult=0.40)),
    ("size 0.50", ORB_BASE.with_(stop_adr_mult=0.50)),
    ("size 0.60", ORB_BASE.with_(stop_adr_mult=0.60)),
    ("BASE size 0.75", ORB_BASE),
    ("size 1.00", ORB_BASE.with_(stop_adr_mult=1.00)),
    ("size 1.25", ORB_BASE.with_(stop_adr_mult=1.25)),
    ("size 1.50", ORB_BASE.with_(stop_adr_mult=1.50)),
    ("size 2.00", ORB_BASE.with_(stop_adr_mult=2.00)),
    ("no stop", ORB_BASE.with_(stop_adr_mult=math.inf)),
    ("trail 0.50", ORB_BASE.with_(trail_adr_mult=0.50)),
    ("trail 0.75", ORB_BASE.with_(trail_adr_mult=0.75)),
    ("trail 1.00", ORB_BASE.with_(trail_adr_mult=1.00)),
    ("close-stop 0.75", ORB_BASE.with_(stop_on_close=True)),
    ("close-stop 0.50", ORB_BASE.with_(stop_on_close=True, stop_adr_mult=0.50)),
    ("time-stop 30m", ORB_BASE.with_(time_stop_minutes=30)),
    ("time-stop 60m", ORB_BASE.with_(time_stop_minutes=60)),
    ("time-stop 120m", ORB_BASE.with_(time_stop_minutes=120)),
]


def risk_mult(cfg: OrbConfig) -> float:
    return cfg.stop_adr_mult if math.isfinite(cfg.stop_adr_mult) else NOMINAL_RISK_ADR_MULT


def max_dd(x: np.ndarray) -> float:
    c = np.cumsum(x)
    return float((c - np.maximum.accumulate(c)).min()) if len(c) else 0.0


def longest_losing(x: np.ndarray) -> int:
    best = cur = 0
    for v in x:
        cur = cur + 1 if v < 0 else 0
        best = max(best, cur)
    return best


def run_variant(name: str, cfg: OrbConfig, m1: pd.DataFrame, valid_days) -> tuple[dict, np.ndarray]:
    t = trades_to_frame(simulate(m1, cfg))
    r = (t["net_pts"] / (risk_mult(cfg) * t["adr14"])).to_numpy()
    yrs = pd.to_datetime(t["day"]).dt.year.to_numpy()
    by_year = {int(y): float(r[yrs == y].sum()) for y in sorted(set(yrs))}
    reasons = t["exit_reason"].value_counts(normalize=True)
    row = dict(
        variant=name, cost_bps=cfg.cost_bps_roundtrip, n=len(t),
        mean_pts=round(float(t["net_pts"].mean()), 2), mean_R=round(float(r.mean()), 4),
        sum_R=round(float(r.sum()), 1), win_pct=round(100 * float((r > 0).mean()), 1),
        maxDD_R=round(max_dd(r), 2), worst_trade_R=round(float(r.min()), 2),
        max_losing_streak=longest_losing(r),
        pos_years=f"{sum(v > 0 for v in by_year.values())}/{len(by_year)}",
        R_2019_22=round(float(r[yrs < SPLIT_YEAR].mean()), 4),
        R_2023_26=round(float(r[yrs >= SPLIT_YEAR].mean()), 4),
        stopped_pct=round(100 * float(1 - reasons.get("time", 0.0)), 1),
        years=" ".join(f"{y}:{v:+.1f}" for y, v in by_year.items()),
    )
    per_day = pd.Series(r, index=t["day"]).groupby(level=0).sum()
    daily = np.array([float(per_day.get(d, 0.0)) for d in valid_days])
    return row, daily


if __name__ == "__main__":
    m1 = load_nsxusd_m1(DATA_DIR)
    base = trades_to_frame(simulate(m1, ORB_BASE))
    ok = len(base) == REGRESSION_N and abs(base["net_pts"].sum() - REGRESSION_SUM_NET_PTS) < 1e-3
    print(f"Regression: n={len(base)} sum={base['net_pts'].sum():.3f} -> {'OK' if ok else 'FAIL'}")
    if not ok:
        raise SystemExit("Regression FAILED")
    valid_days = compute_adr14(compute_daily_sessions(m1, ORB_BASE), ORB_BASE).dropna().index

    # Optional slice "a:b" of VARIANTS (long runs can be split); per-variant RNG seeded
    # identically, so every variant's Gate 3 numbers are independent of the slicing.
    a, b = (int(x) for x in sys.argv[1].split(":")) if len(sys.argv) > 1 else (0, len(VARIANTS))
    suffix = f"_part{a:02d}" if len(sys.argv) > 1 else ""
    rows, gate3 = [], []
    for name, cfg in VARIANTS[a:b]:
        rng = np.random.default_rng(SOLO_SEED)
        for bps in COST_STRESS_BPS:
            row, daily = run_variant(name, cfg.with_(cost_bps_roundtrip=bps), m1, valid_days)
            rows.append(row)
            print(f"{name:22s} {bps:.0f}bp n={row['n']} meanR={row['mean_R']:+.4f} pts={row['mean_pts']:+.2f} "
                  f"WR={row['win_pct']} DD={row['maxDD_R']} worst={row['worst_trade_R']} "
                  f"yrs={row['pos_years']} halves={row['R_2019_22']:+.4f}/{row['R_2023_26']:+.4f} "
                  f"streak={row['max_losing_streak']} stopped={row['stopped_pct']}%", flush=True)
            if bps == 1.0:
                st = prop_sim(daily, SOLO_RISK_PCT, rng)
                st.update(variant=name, risk_pct=SOLO_RISK_PCT)
                gate3.append(st)
                print(f"    Gate3 @{SOLO_RISK_PCT}%: cashout={st['cashout_pct']:.1f}% "
                      f"dd-bust={st['bust_dd_pct']:.1f}% daily-bust={st['bust_daily_pct']:.1f}% "
                      f"timeout={st['timeout_pct']:.1f}% med.days={st['median_days_cashout']:.0f}", flush=True)
                print(f"    years: {row['years']}", flush=True)

    out = Path(__file__).resolve().parent.parent / "reports"
    pd.DataFrame(rows).to_csv(out / f"s021_stops_gate1{suffix}.csv", index=False)
    pd.DataFrame(gate3).to_csv(out / f"s021_stops_gate3_solo{suffix}.csv", index=False)
    print("written reports/s021_stops_gate1.csv, reports/s021_stops_gate3_solo.csv")
