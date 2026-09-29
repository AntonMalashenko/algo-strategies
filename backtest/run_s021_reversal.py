"""S021 reversal modifier (reversal_mode): Gate 0 regression, Gate 1 backtest sweep,
per-leg decomposition, walk-forward-style halves, cost stress, solo and combo Gate 3.

Requested by Anton, 2026-09-28: "сделай бектест разворотов для 021. какие варианты
разворота могут быть?" -- four reversal variants were formalized (see config.py's
reversal_mode field comment): FADE (trade against the breakout), SAR (stop-and-reverse),
FLIP@opposite band, FLIP@session open (failed-breakout fade).

VERDICT (2026-09-28): all four REJECTED -- see config.py's reversal_mode field comment.

Daily R here SUMS both legs of a day (build_daily_R() in run_s021_propscheme.py keeps a
single value per day, which is right for ORB_BASE but would drop reversal legs), so the
prop simulators get the true per-day P&L.

Usage: python3 backtest/run_s021_reversal.py
"""
from __future__ import annotations

import os
import sys
from datetime import time
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from strategies.orb_intraday.config import (
    ORB_BASE, ORB_REV_FADE, ORB_REV_SAR, ORB_REV_FLIP_OPP, ORB_REV_FLIP_OPEN, OrbConfig,
)
from strategies.orb_intraday.engine import (
    load_nsxusd_m1, compute_daily_sessions, compute_adr14, simulate, trades_to_frame,
)
from backtest.run_s021_propscheme import prop_sim, DATA_DIR, SEED as SOLO_SEED
from backtest.run_s007_s021_combo_propscheme import (
    build_s007_frame, align, joint_prop_sim, S007_LIVE_RISK_PCT, S021_LIVE_RISK_PCT,
    SEED as COMBO_SEED,
)

REGRESSION_N = 1518
REGRESSION_SUM_NET_PTS = 16220.614
SOLO_RISK_LEVELS = (0.50, 0.75)
COST_STRESS_BPS = (1.0, 3.0)
SPLIT_YEAR = 2023                      # first half 2019-2022, second half 2023-2026
LATE_CUTOFF = time(15, 29)             # sensitivity: allow reversal legs later in the day

VARIANTS: list[tuple[str, OrbConfig]] = [
    ("BASE", ORB_BASE),
    ("FADE", ORB_REV_FADE),
    ("SAR", ORB_REV_SAR),
    ("FLIP@band", ORB_REV_FLIP_OPP),
    ("FLIP@open", ORB_REV_FLIP_OPEN),
    ("SAR late", ORB_REV_SAR.with_(reversal_cutoff=LATE_CUTOFF)),
    ("FLIP@band late", ORB_REV_FLIP_OPP.with_(reversal_cutoff=LATE_CUTOFF)),
    ("FLIP@open late", ORB_REV_FLIP_OPEN.with_(reversal_cutoff=LATE_CUTOFF)),
]


def regression_check(m1: pd.DataFrame) -> None:
    t = trades_to_frame(simulate(m1, ORB_BASE))
    n, s = len(t), round(float(t["net_pts"].sum()), 3)
    ok = n == REGRESSION_N and abs(s - REGRESSION_SUM_NET_PTS) < 1e-3 and (t["leg"] == 1).all()
    print(f"Regression: n={n} sum_net_pts={s:.3f} -> {'OK' if ok else 'FAIL'}")
    if not ok:
        raise SystemExit("Regression FAILED -- reversal code path changed ORB_BASE.")


def trades_R(t: pd.DataFrame, cfg: OrbConfig) -> pd.Series:
    return t["net_pts"] / (cfg.stop_adr_mult * t["adr14"])


def daily_R_series(m1: pd.DataFrame, cfg: OrbConfig, t: pd.DataFrame) -> pd.Series:
    daily = compute_daily_sessions(m1, cfg)
    valid_days = compute_adr14(daily, cfg).dropna().index
    r = trades_R(t, cfg).groupby(t["day"]).sum() if len(t) else pd.Series(dtype=float)
    return pd.Series([float(r.get(d, 0.0)) for d in valid_days], index=pd.to_datetime(valid_days))


def max_dd(x: np.ndarray) -> float:
    c = np.cumsum(x)
    return float((c - np.maximum.accumulate(c)).min()) if len(c) else 0.0


def gate1(name: str, cfg: OrbConfig, m1: pd.DataFrame) -> tuple[dict, pd.DataFrame]:
    t = trades_to_frame(simulate(m1, cfg))
    t["R"] = trades_R(t, cfg)
    day_R = t.groupby("day")["R"].sum()
    yrs = day_R.groupby(pd.to_datetime(day_R.index).year).sum()
    l1, l2 = t[t["leg"] == 1], t[t["leg"] == 2]
    first = day_R[pd.to_datetime(day_R.index).year < SPLIT_YEAR]
    second = day_R[pd.to_datetime(day_R.index).year >= SPLIT_YEAR]
    row = dict(
        variant=name, trades=len(t), trade_days=len(day_R),
        sum_R=round(float(t["R"].sum()), 1), R_per_trade_day=round(float(day_R.mean()), 4),
        sum_pts=round(float(t["net_pts"].sum()), 0),
        day_win_pct=round(100 * float((day_R > 0).mean()), 1),
        maxDD_R=round(max_dd(day_R.to_numpy()), 2),
        worst_day_R=round(float(day_R.min()), 2),
        pos_years=f"{int((yrs > 0).sum())}/{len(yrs)}",
        R_2019_22=round(float(first.mean()), 4), R_2023_26=round(float(second.mean()), 4),
        leg1_n=len(l1), leg1_meanR=round(float(l1["R"].mean()), 4),
        leg2_n=len(l2), leg2_meanR=round(float(l2["R"].mean()), 4) if len(l2) else np.nan,
        leg2_win_pct=round(100 * float((l2["R"] > 0).mean()), 1) if len(l2) else np.nan,
        years=" ".join(f"{y}:{v:+.1f}" for y, v in yrs.items()),
    )
    return row, t


if __name__ == "__main__":
    m1 = load_nsxusd_m1(DATA_DIR)
    print(f"NSXUSD M1 {m1.index.min()} .. {m1.index.max()} ({len(m1)} bars)")
    regression_check(m1)

    rows, per_variant_trades = [], {}
    for name, cfg in VARIANTS:
        for bps in COST_STRESS_BPS:
            c = cfg.with_(cost_bps_roundtrip=bps)
            row, t = gate1(name, c, m1)
            row["cost_bps"] = bps
            rows.append(row)
            if bps == 1.0:
                per_variant_trades[name] = (c, t)
    g1 = pd.DataFrame(rows)
    pd.set_option("display.width", 250, "display.max_columns", 30)
    print(g1.drop(columns=["years"]).to_string(index=False))
    print()
    for r in rows:
        if r["cost_bps"] == 1.0:
            print(f"{r['variant']:15s} {r['years']}")

    print("\n=== Gate 3 solo ===")
    solo_rows = []
    rng = np.random.default_rng(SOLO_SEED)
    for name, (cfg, t) in per_variant_trades.items():
        s = daily_R_series(m1, cfg, t).to_numpy()
        for risk in SOLO_RISK_LEVELS:
            st = prop_sim(s, risk, rng)
            st.update(variant=name, risk_pct=risk)
            solo_rows.append(st)
            print(f"{name:15s} risk={risk:.2f}% cashout={st['cashout_pct']:5.1f}% "
                  f"daily-bust={st['bust_daily_pct']:4.1f}% dd-bust={st['bust_dd_pct']:4.1f}% "
                  f"timeout={st['timeout_pct']:4.1f}% med.days={st['median_days_cashout']:.0f}")

    print("\n=== Gate 3 combo (S007 live + S021 variant) ===")
    s007_dates, s007_R, s007_mask = build_s007_frame()
    combo_rows = []
    rng = np.random.default_rng(COMBO_SEED)
    for name, (cfg, t) in per_variant_trades.items():
        s021 = daily_R_series(m1, cfg, t)
        cal, p, m, a = align(s007_dates, s007_R, s007_mask, s021)
        st = joint_prop_sim(p, m, a, S007_LIVE_RISK_PCT, S021_LIVE_RISK_PCT, rng)
        st.update(variant=name)
        combo_rows.append(st)
        print(f"{name:15s} cashout={st['cashout_pct']:6.2f}% daily-bust={st['bust_daily_pct']:5.2f}% "
              f"dd-bust={st['bust_dd_pct']:5.2f}% med.days={st['median_days_cashout']:.0f}")

    out = Path(__file__).resolve().parent.parent / "reports"
    g1.to_csv(out / "s021_reversal_gate1.csv", index=False)
    pd.DataFrame(solo_rows).to_csv(out / "s021_reversal_gate3_solo.csv", index=False)
    pd.DataFrame(combo_rows).to_csv(out / "s021_reversal_gate3_combo.csv", index=False)
    print("\nwritten reports/s021_reversal_gate1.csv, _gate3_solo.csv, _gate3_combo.csv")
