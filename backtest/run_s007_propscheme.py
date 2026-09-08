"""Prop-firm survivability simulation: "maximally prop" scheme (Anton, 2026-09-02).

Compares two position-granularity schemes at HALVED risk, each swept with/without
breakeven:

  A) 4 slots @ 0.5%/R   (WORKING_S007_NEWSSAFE, max_positions=4 -- existing/live shape)
  B) 8 slots @ 0.25%/R  (WORKING_S007_NEWSSAFE_MAX8, max_positions=8 -- NEW, never
                          backtested before this script; doubles the pyramiding cap)

Both intend the SAME total per-day risk budget (4*0.5%=8*0.25%=2% if every slot fills),
split into coarser vs finer pieces -- the question is whether finer granularity changes
prop-firm survivability (daily loss limit / total drawdown / cashout), not raw R.

EXECUTION-FRICTION MODEL (Anton, 2026-09-02, explicit instruction): don't build real
fill-probability mechanics into the bar-by-bar engine -- model slippage/requotes/latency
by simply dropping a fraction of the historically-realized positions. Anton's own numbers
("3 of 4 trades", "6 of 8 trades") are both exactly 75%, so FILL_PROB=0.75 is applied
per INDIVIDUAL position (primary entry, every add, and every recovery-leg position),
independently, each time that historical day is drawn in the simulation below -- not a
fixed "always drop the last one" rule, so the SAME historical day contributes different
realized R across different simulated account paths (this is also what makes "an average
of 3 of 4" a distribution rather than a deterministic day-count).

GATE 3 METHODOLOGY: block-bootstrap Monte Carlo over the historical trading-day sequence
(block=5 consecutive trading days, to keep day-to-day serial structure instead of
independently shuffling days), same %-thresholds as strategy-spec-S007.md sec 10.3's
original prop model (cashout +9%, daily loss limit -3%, total drawdown -10%) -- rebuilt
from scratch here because the original prop_sim.py script was lost (sec 12 of that doc).
Outcome classified per simulated account path: which threshold is hit first, and after
how many trading days. "Trading day" here = a day the strategy had a signal (scenario A
or B) in the historical data; no-signal calendar days are not modelled as separate zero
days, so "days" below means signal-days, not calendar days.

Usage: python3 backtest/run_s007_propscheme.py
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

REAL_SPREAD_PER_SIDE = 0.635   # Gate 2 standard (1.27pt round-trip / 2), points cost model
FILL_PROB = 0.75               # pre-declared: "3 of 4" == "6 of 8" == 0.75 (Anton, 2026-09-02)
BE_LEVELS = [None, 0.5, 1.0, 1.5, 2.0]

CASHOUT_PCT = 9.0               # account target
DAILY_LIMIT_PCT = -3.0          # single-day loss limit
TOTAL_DD_PCT = 10.0             # max drawdown from equity peak

BLOCK_LEN = 5                    # trading days per bootstrap block (proxy for a week)
T_MAX = 500                      # simulated trading-day cap per account path (timeout)
N_SIM = 4000                     # Monte Carlo account paths per combo
SEED = 20260902                  # reproducibility


def positions_matrix(res: pd.DataFrame) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """From engine run() output, build per-day arrays of individual (cost-applied,
    risk_per_add-weighted) position R contributions, padded to the widest day.
    Returns (dates, pos_R[D,MAXP], pos_mask[D,MAXP])."""
    res = res.sort_values("date").reset_index(drop=True)
    day_lists = []
    for _, row in res.iterrows():
        rs = []
        for p in row["positions"]:
            w = 1.0  # risk_per_add==1.0 in every config used here; kept explicit for clarity
            rs.append(w * p["R"])
        day_lists.append(rs)
    maxp = max((len(x) for x in day_lists), default=0)
    D_ = len(day_lists)
    pos_R = np.zeros((D_, maxp), dtype=float)
    pos_mask = np.zeros((D_, maxp), dtype=bool)
    for i, rs in enumerate(day_lists):
        pos_R[i, :len(rs)] = rs
        pos_mask[i, :len(rs)] = True
    return res["date"].to_numpy(), pos_R, pos_mask


def prop_sim(pos_R: np.ndarray, pos_mask: np.ndarray, risk_pct: float,
             rng: np.random.Generator) -> dict:
    """Vectorized block-bootstrap prop simulation. Returns aggregate outcome stats
    across N_SIM independent simulated account paths."""
    Dn, maxp = pos_R.shape
    n_blocks = int(np.ceil(T_MAX / BLOCK_LEN))
    starts = rng.integers(0, max(Dn - BLOCK_LEN, 1), size=(N_SIM, n_blocks))
    offsets = np.arange(BLOCK_LEN)
    path_idx = (starts[:, :, None] + offsets[None, None, :]).reshape(N_SIM, -1)[:, :T_MAX]
    path_idx = np.clip(path_idx, 0, Dn - 1)

    day_pos_R = pos_R[path_idx]        # (N_SIM, T_MAX, maxp)
    day_pos_mask = pos_mask[path_idx]  # (N_SIM, T_MAX, maxp)

    fill = rng.random(day_pos_R.shape) < FILL_PROB
    kept = fill & day_pos_mask
    day_R = np.where(kept, day_pos_R, 0.0).sum(axis=2)          # (N_SIM, T_MAX)
    day_pct = day_R * risk_pct

    # fill-rate sanity check: realized kept-positions / available-positions
    avail = day_pos_mask.sum()
    realized_fill_rate = float(kept.sum() / avail) if avail > 0 else float("nan")

    cum_equity = np.cumsum(day_pct, axis=1)
    running_peak = np.maximum.accumulate(cum_equity, axis=1)
    dd = running_peak - cum_equity

    daily_breach = day_pct <= DAILY_LIMIT_PCT
    dd_breach = dd >= TOTAL_DD_PCT
    cashout_flag = cum_equity >= CASHOUT_PCT
    stop = daily_breach | dd_breach | cashout_flag

    any_stop = stop.any(axis=1)
    first_idx = np.where(any_stop, np.argmax(stop, axis=1), T_MAX - 1)
    rows = np.arange(N_SIM)

    outcome = np.full(N_SIM, "timeout", dtype=object)
    is_daily = daily_breach[rows, first_idx] & any_stop
    is_dd = dd_breach[rows, first_idx] & any_stop & ~is_daily
    is_cash = cashout_flag[rows, first_idx] & any_stop & ~is_daily & ~is_dd
    outcome[is_daily] = "bust_daily"
    outcome[is_dd] = "bust_dd"
    outcome[is_cash] = "cashout"

    days_to_outcome = first_idx + 1
    worst_day_pct = float(day_pct.min())

    n = N_SIM
    return dict(
        cashout_pct=100 * float((outcome == "cashout").mean()),
        bust_daily_pct=100 * float((outcome == "bust_daily").mean()),
        bust_dd_pct=100 * float((outcome == "bust_dd").mean()),
        timeout_pct=100 * float((outcome == "timeout").mean()),
        median_days_cashout=(float(np.median(days_to_outcome[outcome == "cashout"]))
                              if (outcome == "cashout").any() else float("nan")),
        median_days_bust=(float(np.median(days_to_outcome[np.isin(outcome, ["bust_daily", "bust_dd"])]))
                           if np.isin(outcome, ["bust_daily", "bust_dd"]).any() else float("nan")),
        worst_day_pct=worst_day_pct,
        realized_fill_rate=realized_fill_rate,
        n=n,
    )


def fmt(stats: dict, label: str) -> str:
    return (f"{label:34s} cashout {stats['cashout_pct']:5.1f}%  "
            f"daily-bust {stats['bust_daily_pct']:5.1f}%  dd-bust {stats['bust_dd_pct']:5.1f}%  "
            f"timeout {stats['timeout_pct']:4.1f}%  "
            f"med.days(cash/bust) {stats['median_days_cashout']:5.0f}/{stats['median_days_bust']:5.0f}  "
            f"worst-day {stats['worst_day_pct']:+6.2f}%  fill~{stats['realized_fill_rate']:.3f}")


if __name__ == "__main__":
    df = D.load("duka")
    lv = D.daily_levels(df)
    print(f"Dukascopy data: {df['date_only'].min()} .. {df['date_only'].max()} ({len(df)} bars)")
    print(f"FILL_PROB={FILL_PROB}  block={BLOCK_LEN}  T_MAX={T_MAX}  N_SIM={N_SIM}  "
          f"thresholds: cashout {CASHOUT_PCT}% / daily {DAILY_LIMIT_PCT}% / totalDD {TOTAL_DD_PCT}%\n")

    SCHEMES = [
        ("A: 4 slots @ 0.5%/R ", C.WORKING_S007_NEWSSAFE.with_(spread_per_side=REAL_SPREAD_PER_SIDE), 0.5),
        ("B: 8 slots @ 0.25%/R", C.WORKING_S007_NEWSSAFE_MAX8.with_(spread_per_side=REAL_SPREAD_PER_SIDE), 0.25),
    ]

    rng = np.random.default_rng(SEED)
    all_rows = []
    for label, base_cfg, risk_pct in SCHEMES:
        print(f"=== {label} (max_positions={base_cfg.max_positions}) ===")
        for be in BE_LEVELS:
            cfg = base_cfg if be is None else base_cfg.with_(breakeven_at_r=be)
            res = run(df, cfg, lv)
            n_days = len(res)
            avg_pos = res["n_pos"].mean() if n_days else float("nan")
            dates, pos_R, pos_mask = positions_matrix(res)
            stats = prop_sim(pos_R, pos_mask, risk_pct, rng)
            be_label = "no BU" if be is None else f"BU@{be}R"
            row_label = f"  {be_label:10s}"
            print(f"{row_label} [{n_days} signal-days, avg {avg_pos:.2f} pos/day]  " + fmt(stats, ""))
            stats.update(scheme=label.strip(), be=be_label, n_days=n_days, avg_pos=avg_pos,
                         risk_pct=risk_pct, max_positions=base_cfg.max_positions)
            all_rows.append(stats)
        print()

    summary = pd.DataFrame(all_rows)
    print("=== Summary, sorted by cashout% within each scheme ===")
    for label, _, _ in SCHEMES:
        sub = summary[summary["scheme"] == label.strip()].sort_values("cashout_pct", ascending=False)
        print(f"\n{label}:")
        for _, r in sub.iterrows():
            print(f"  {r['be']:10s} cashout {r['cashout_pct']:5.1f}%  daily-bust {r['bust_daily_pct']:5.1f}%  "
                  f"dd-bust {r['bust_dd_pct']:5.1f}%  worst-day {r['worst_day_pct']:+6.2f}%  "
                  f"med.days-cash {r['median_days_cashout']:5.0f}")
