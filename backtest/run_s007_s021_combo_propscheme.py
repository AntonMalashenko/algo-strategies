"""Joint Gate 3 (prod-reality / prop-firm simulation) for the S007+S021 COMBO --
both strategies already trade on the SAME live cTrader account (ctrader-47939312,
demo) but their risk has never been modeled TOGETHER. Requested by Anton,
2026-09-28: "давай протестируем связку 007 + 021 для пропа. найди максимально
выгодную комбинацию риска для прохождения челленджей и прибыльной торговли".

Reuses both strategies' own already-validated Gate 3 building blocks --
positions_matrix() from run_s007_propscheme.py (per-day, per-position R for S007's
pyramid engine) and build_daily_R() from run_s021_propscheme.py (per-day single R
for S021's one-trade-a-day engine) -- rather than re-deriving them, per
code-architecture's single-source-of-truth rule.

WHY A SEPARATE SCRIPT INSTEAD OF JUST ADDING THE TWO SOLO GATE-3 RESULTS: each solo
script block-bootstraps its OWN trading-day sequence independently. Summing two
independently-resampled outcomes would implicitly assume the two legs' daily P&L is
day-to-day independent -- true only if they were drawn from separate random
calendars, false in reality: S007 and S021 share ONE account and the SAME calendar
dates (a violent macro day can move DAX/GER40 in the European morning AND Nasdaq in
the US afternoon on the same date). So this script draws ONE shared sequence of
block start-indices over the ALIGNED calendar-date axis and looks up BOTH legs' R
at that same date -- preserving whatever real joint day-to-day structure exists
instead of assuming independence and understating combined tail risk.

DATA WINDOW: the two data sources only partially overlap -- Dukascopy GER40 M1 for
S007 starts 2023-06-26 (ends 2026-08-11) while histdata NSXUSD M1 for S021 runs
2019-01-01..2026-06-26. The joint simulation uses the INTERSECTION only,
2023-06-26..2026-06-26 (~3 years) -- the only period where both legs' historical R
is simultaneously known. This is shorter than either strategy's own solo Gate 3
window; an honest limitation of the joint analysis, not a choice.

CORRELATION: reports the same class of number this project already computes for
other strategy pairs sharing an account/instrument (S022.1 vs S022, S023 vs S021,
S024.1 vs S024 -- strategy-modifiers/strategy-lifecycle's Gate 3 convention) --
daily R-correlation between the two legs, zero-filled on no-trade days, over the
shared calendar-date axis.

S007 LIVE PRESET NOTE: as of this script (2026-09-28), bot/s007_config.py::PRESET
is WORKING_S007_NEWSSAFE_MAX8_BE05_OFF2 (8 slots @0.25%/R + BE@0.5R with a 2pt
breakeven offset) -- the "maximally prop" scheme from strategy-passport-S007.md
sec 4d/ALGODEV-37/ALGODEV-43. That passport (last touched 2026-09-02) still
describes this scheme as "candidate, not yet promoted" -- that line is now stale;
the live bot has run this exact preset for weeks. This script uses the true live
preset, not the stale passport's caveat, and the docs update this script feeds
into should fix that line.

Usage: python3 backtest/run_s007_s021_combo_propscheme.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from strategies.ger40_lonfra import config as C
from strategies.ger40_lonfra import data as D
from strategies.ger40_lonfra.engine import run as run_s007
from strategies.orb_intraday.config import ORB_BASE
from strategies.orb_intraday.engine import (
    load_nsxusd_m1, compute_daily_sessions, compute_adr14,
)

from backtest.run_s007_propscheme import positions_matrix, REAL_SPREAD_PER_SIDE as S007_REAL_SPREAD_PER_SIDE
from backtest.run_s021_propscheme import build_daily_R

DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "histdata"

# S007 live preset -- see module docstring's "S007 LIVE PRESET NOTE" above.
S007_LIVE_PRESET = C.WORKING_S007_NEWSSAFE_MAX8_BE05_OFF2
S007_LIVE_RISK_PCT = 0.25   # bot/s007_config.py::RISK_PCT

S021_LIVE_RISK_PCT = 0.5    # bot/orb_config.py::RISK_PCT (mid of Gate 3's own 0.50-0.75% range)

CASHOUT_PCT = 9.0
DAILY_LIMIT_PCT = -3.0
TOTAL_DD_PCT = 10.0

S007_FILL_PROB = 0.75   # unchanged from run_s007_propscheme.py -- cross-checked vs live demo fills
S021_FILL_PROB = 1.0    # unchanged from run_s021_propscheme.py -- no measured degradation figure yet

BLOCK_LEN = 5
T_MAX = 500
N_SIM = 4000
SEED = 20260928

S007_RISK_GRID = [0.20, 0.25, 0.30]
S021_RISK_GRID = [0.25, 0.50, 0.75, 1.00]


def build_s007_frame():
    df = D.load("duka")
    lv = D.daily_levels(df)
    cfg = S007_LIVE_PRESET.with_(spread_per_side=S007_REAL_SPREAD_PER_SIDE)
    res = run_s007(df, cfg, lv)
    dates, pos_R, pos_mask = positions_matrix(res)
    return pd.to_datetime(dates), pos_R, pos_mask


def build_s021_series():
    m1 = load_nsxusd_m1(DATA_DIR)
    r_series = build_daily_R(m1, ORB_BASE)
    daily = compute_daily_sessions(m1, ORB_BASE)
    adr14 = compute_adr14(daily, ORB_BASE)
    valid_days = adr14.dropna().index
    return pd.Series(r_series, index=pd.to_datetime(valid_days))


def align(s007_dates, s007_pos_R, s007_pos_mask, s021_series):
    lo = max(s007_dates.min(), s021_series.index.min())
    hi = min(s007_dates.max(), s021_series.index.max())
    calendar = pd.bdate_range(lo, hi)

    maxp = s007_pos_R.shape[1]
    s007_pos_by_date = pd.DataFrame(
        np.where(s007_pos_mask, s007_pos_R, np.nan), index=s007_dates
    )
    s007_pos_aligned = s007_pos_by_date.reindex(calendar).to_numpy()
    s007_mask_aligned = ~np.isnan(s007_pos_aligned)
    s007_pos_aligned = np.where(s007_mask_aligned, s007_pos_aligned, 0.0)

    s021_aligned = s021_series.reindex(calendar, fill_value=0.0).to_numpy()
    return calendar, s007_pos_aligned, s007_mask_aligned, s021_aligned


def joint_prop_sim(s007_pos_R, s007_pos_mask, s021_R, s007_risk_pct, s021_risk_pct, rng):
    Dn = len(s021_R)
    n_blocks = int(np.ceil(T_MAX / BLOCK_LEN))
    starts = rng.integers(0, max(Dn - BLOCK_LEN, 1), size=(N_SIM, n_blocks))
    offsets = np.arange(BLOCK_LEN)
    path_idx = (starts[:, :, None] + offsets[None, None, :]).reshape(N_SIM, -1)[:, :T_MAX]
    path_idx = np.clip(path_idx, 0, Dn - 1)

    day_pos_R = s007_pos_R[path_idx]
    day_pos_mask = s007_pos_mask[path_idx]
    fill7 = rng.random(day_pos_R.shape) < S007_FILL_PROB
    s007_day_R = np.where(fill7 & day_pos_mask, day_pos_R, 0.0).sum(axis=2)

    day_s021_R = s021_R[path_idx]
    fill21 = rng.random(day_s021_R.shape) < S021_FILL_PROB
    day_s021_R = np.where((day_s021_R != 0.0) & fill21, day_s021_R,
                           np.where(day_s021_R != 0.0, 0.0, day_s021_R))

    day_pct = s007_day_R * s007_risk_pct + day_s021_R * s021_risk_pct

    cum_equity = np.cumsum(day_pct, axis=1)
    running_peak = np.maximum.accumulate(cum_equity, axis=1)
    dd = running_peak - cum_equity

    daily_breach = day_pct <= DAILY_LIMIT_PCT
    dd_breach = dd >= TOTAL_DD_PCT
    cashout_flag = cum_equity >= CASHOUT_PCT
    stop = daily_breach | dd_breach | cashout_flag

    any_stop = stop.any(axis=1)
    first_idx = np.where(any_stop, np.argmax(stop, axis=1), T_MAX - 1)
    rows = np.arange(len(s021_R)) if False else np.arange(day_pct.shape[0])

    outcome = np.full(day_pct.shape[0], "timeout", dtype=object)
    is_daily = daily_breach[rows, first_idx] & any_stop
    is_dd = dd_breach[rows, first_idx] & any_stop & ~is_daily
    is_cash = cashout_flag[rows, first_idx] & any_stop & ~is_daily & ~is_dd
    outcome[is_daily] = "bust_daily"
    outcome[is_dd] = "bust_dd"
    outcome[is_cash] = "cashout"

    days_to_outcome = first_idx + 1
    return dict(
        cashout_pct=100 * float((outcome == "cashout").mean()),
        bust_daily_pct=100 * float((outcome == "bust_daily").mean()),
        bust_dd_pct=100 * float((outcome == "bust_dd").mean()),
        timeout_pct=100 * float((outcome == "timeout").mean()),
        median_days_cashout=(float(np.median(days_to_outcome[outcome == "cashout"]))
                              if (outcome == "cashout").any() else float("nan")),
        worst_day_pct=float(day_pct.min()),
        mean_day_pct=float(day_pct.mean()),
    )


if __name__ == "__main__":
    s007_dates, s007_pos_R_raw, s007_pos_mask_raw = build_s007_frame()
    s021_series = build_s021_series()
    calendar, s007_pos_aligned, s007_mask_aligned, s021_aligned = align(
        s007_dates, s007_pos_R_raw, s007_pos_mask_raw, s021_series
    )

    print(f"Joint window: {calendar.min().date()} .. {calendar.max().date()} ({len(calendar)} business days)")
    s007_day_sum = np.where(s007_mask_aligned, s007_pos_aligned, 0.0).sum(axis=1)
    both_trade = (s007_day_sum != 0) & (s021_aligned != 0)
    only_s007 = (s007_day_sum != 0) & (s021_aligned == 0)
    only_s021 = (s007_day_sum == 0) & (s021_aligned != 0)
    neither = (s007_day_sum == 0) & (s021_aligned == 0)
    corr = float(np.corrcoef(s007_day_sum, s021_aligned)[0, 1])
    print(f"S007 signal-days: {(s007_day_sum != 0).sum()}/{len(calendar)}  "
          f"S021 trade-days: {(s021_aligned != 0).sum()}/{len(calendar)}")
    print(f"both-trade days: {both_trade.sum()} ({100 * both_trade.mean():.1f}%)  "
          f"only-S007: {only_s007.sum()}  only-S021: {only_s021.sum()}  neither: {neither.sum()}")
    print(f"Daily R correlation (S007 day-sum vs S021, zero-filled): {corr:+.4f}\n")

    rng = np.random.default_rng(SEED)
    rows_out = []
    print("=== Combo Gate 3 sweep (S007 risk% x S021 risk%) ===")
    for s7 in S007_RISK_GRID:
        for s21 in S021_RISK_GRID:
            stats = joint_prop_sim(s007_pos_aligned, s007_mask_aligned, s021_aligned, s7, s21, rng)
            print(f"  S007={s7:.2f}%  S021={s21:.2f}%  cashout {stats['cashout_pct']:5.1f}%  "
                  f"daily-bust {stats['bust_daily_pct']:5.1f}%  dd-bust {stats['bust_dd_pct']:5.1f}%  "
                  f"worst-day {stats['worst_day_pct']:+6.2f}%  mean-day {stats['mean_day_pct']:+.4f}%  "
                  f"med.days-cash {stats['median_days_cashout']:5.0f}")
            stats.update(s007_risk_pct=s7, s021_risk_pct=s21)
            rows_out.append(stats)

    print("\n=== Solo baselines on the SAME joint window (comparability check) ===")
    for s7 in S007_RISK_GRID:
        stats = joint_prop_sim(s007_pos_aligned, s007_mask_aligned, np.zeros_like(s021_aligned), s7, 0.0, rng)
        print(f"  S007 alone={s7:.2f}%  cashout {stats['cashout_pct']:5.1f}%  daily-bust {stats['bust_daily_pct']:5.1f}%  "
              f"dd-bust {stats['bust_dd_pct']:5.1f}%  worst-day {stats['worst_day_pct']:+6.2f}%")
    for s21 in S021_RISK_GRID:
        stats = joint_prop_sim(np.zeros_like(s007_pos_aligned), np.zeros_like(s007_mask_aligned), s021_aligned, 0.0, s21, rng)
        print(f"  S021 alone={s21:.2f}%  cashout {stats['cashout_pct']:5.1f}%  daily-bust {stats['bust_daily_pct']:5.1f}%  "
              f"dd-bust {stats['bust_dd_pct']:5.1f}%  worst-day {stats['worst_day_pct']:+6.2f}%")

    out = pd.DataFrame(rows_out)
    out_csv = Path(__file__).resolve().parent.parent / "reports" / "s007_s021_combo_propscheme_summary.csv"
    out_csv.parent.mkdir(exist_ok=True)
    out.to_csv(out_csv, index=False)
    print(f"\nSummary written to {out_csv}")
