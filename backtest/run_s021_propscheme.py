"""Gate 3 (prod-reality / prop-firm simulation) for S021 -- ORB / intraday momentum,
Nasdaq 100 (strategy-lifecycle skill sec "validation gates"; passport
claude/strategy-passport-S021.md sec 9, item 1, requested by Anton 2026-09-21).

Reuses the block-bootstrap Monte Carlo methodology from run_s007_propscheme.py
(same thresholds, same BLOCK_LEN/T_MAX/N_SIM/SEED, same prop_sim() shape), adapted
to S021's much simpler trade structure: at most 1 trade/day, no pyramiding, no
breakeven-level sweep (S021 has none), so "positions per day" collapses to a
single R value per calendar trading day (0.0 on a day with a valid session but no
breakout/no trade).

R-multiple definition: each trade's stop distance is stop_adr_mult * ADR14 points
(cfg.py), so R = net_pts / (stop_adr_mult * ADR14) for that trade's own ADR14 --
this is the natural per-trade risk unit for a fixed-%-risk position-sizing scheme
(lots sized so stop distance == risk_pct of equity), matching how S007's own prop
scheme is expressed in R.

Day sequence: built from every day with a valid causal ADR14 (i.e. every day the
strategy COULD have traded), not just days with an actual entry -- this preserves
S021's true ~94%-of-days trade frequency inside the block-bootstrap instead of
silently only sampling active days (unlike run_s007_propscheme.py, where "trading
day" = signal-day only; S021's frequency is high enough that the difference is
small, but built this way for correctness rather than convenience).

EXECUTION FRICTION: unlike S007 (Anton had a stated "3 of 4 / 6 of 8 trades filled"
heuristic, FILL_PROB=0.75), S021 has no measured real-broker degradation figure yet
-- passport sec 7.3 explicitly flags real spread as NOT measured. Fabricating a
FILL_PROB here would misrepresent an unmeasured quantity as data, so this script
runs FILL_PROB=1.0 (every entry executes as backtested) and instead stress-tests
cost sensitivity directly: reruns the whole R-series at cost_bps_roundtrip=3.0 (vs
the frozen 1.0bp), matching the passport's own sec 4 cost-sensitivity finding ("at
3bp: -25% return, at 5bp: edge disappears") to see how that maps onto prop
survivability specifically, rather than just average edge.

Thresholds reused as-is from run_s007_propscheme.py for direct comparability
(cashout +9%, daily loss limit -3%, total drawdown -10%) -- this is a generic
FTMO/5ers-style prop profile, not yet tied to a specific program Anton has chosen
for S021 (no such choice is recorded in the passport); revisit if/when he picks one.

Usage: python3 backtest/run_s021_propscheme.py
"""
from __future__ import annotations

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

DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "histdata"

CASHOUT_PCT = 9.0                # account target -- same as run_s007_propscheme.py
DAILY_LIMIT_PCT = -3.0           # single-day loss limit
TOTAL_DD_PCT = 10.0              # max drawdown from equity peak
FILL_PROB = 1.0                  # no measured S021 execution-degradation figure yet (sec 7.3) -- full fills

BLOCK_LEN = 5                    # trading days per bootstrap block (proxy for a week)
T_MAX = 500                      # simulated trading-day cap per account path (timeout)
N_SIM = 4000                     # Monte Carlo account paths per combo
SEED = 20260921                  # reproducibility

RISK_PCT_GRID = [0.10, 0.25, 0.50, 0.75, 1.00, 1.50, 2.00]  # %/trade sweep


def build_daily_R(m1: pd.DataFrame, cfg: OrbConfig) -> np.ndarray:
    """One R value per valid trading day (has causal ADR14): the trade's net_pts /
    stop distance if a trade fired that day, else 0.0. Chronological order preserved."""
    daily = compute_daily_sessions(m1, cfg)
    adr14 = compute_adr14(daily, cfg)
    valid_days = adr14.dropna().index
    trades = trades_to_frame(simulate(m1, cfg))
    trades_by_day = {}
    if len(trades):
        trades = trades.set_index("day")
        for d, row in trades.iterrows():
            stop_dist = cfg.stop_adr_mult * row["adr14"]
            trades_by_day[d] = row["net_pts"] / stop_dist if stop_dist > 0 else 0.0
    r_series = np.array([trades_by_day.get(d, 0.0) for d in valid_days], dtype=float)
    return r_series


def prop_sim(r_series: np.ndarray, risk_pct: float, rng: np.random.Generator) -> dict:
    """Block-bootstrap Monte Carlo over the historical day-R sequence. Same shape as
    run_s007_propscheme.py's prop_sim(), specialized to 1 R-value/day (no positions matrix)."""
    Dn = len(r_series)
    n_blocks = int(np.ceil(T_MAX / BLOCK_LEN))
    starts = rng.integers(0, max(Dn - BLOCK_LEN, 1), size=(N_SIM, n_blocks))
    offsets = np.arange(BLOCK_LEN)
    path_idx = (starts[:, :, None] + offsets[None, None, :]).reshape(N_SIM, -1)[:, :T_MAX]
    path_idx = np.clip(path_idx, 0, Dn - 1)

    day_R = r_series[path_idx]                       # (N_SIM, T_MAX)
    fill = rng.random(day_R.shape) < FILL_PROB
    day_R = np.where((day_R != 0.0) & fill, day_R, np.where(day_R != 0.0, 0.0, day_R))
    day_pct = day_R * risk_pct

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
    return dict(
        cashout_pct=100 * float((outcome == "cashout").mean()),
        bust_daily_pct=100 * float((outcome == "bust_daily").mean()),
        bust_dd_pct=100 * float((outcome == "bust_dd").mean()),
        timeout_pct=100 * float((outcome == "timeout").mean()),
        median_days_cashout=(float(np.median(days_to_outcome[outcome == "cashout"]))
                              if (outcome == "cashout").any() else float("nan")),
        median_days_bust=(float(np.median(days_to_outcome[np.isin(outcome, ["bust_daily", "bust_dd"])]))
                           if np.isin(outcome, ["bust_daily", "bust_dd"]).any() else float("nan")),
        worst_day_pct=float(day_pct.min()),
    )


def longest_losing_streak(r_series: np.ndarray) -> int:
    streak = maxstreak = 0
    for r in r_series:
        if r < 0:
            streak += 1
            maxstreak = max(maxstreak, streak)
        elif r > 0:
            streak = 0
        # r == 0 (no trade that day) doesn't reset or extend a losing streak
    return maxstreak


def fmt(stats: dict, label: str) -> str:
    return (f"{label:12s} cashout {stats['cashout_pct']:5.1f}%  "
            f"daily-bust {stats['bust_daily_pct']:5.1f}%  dd-bust {stats['bust_dd_pct']:5.1f}%  "
            f"timeout {stats['timeout_pct']:4.1f}%  "
            f"med.days(cash/bust) {stats['median_days_cashout']:5.0f}/{stats['median_days_bust']:5.0f}  "
            f"worst-day {stats['worst_day_pct']:+6.2f}%")


if __name__ == "__main__":
    m1 = load_nsxusd_m1(DATA_DIR)
    print(f"NSXUSD M1 (fixed-EST anchor, canonical): {m1.index.min()} .. {m1.index.max()} ({len(m1)} bars)")
    print(f"FILL_PROB={FILL_PROB}  block={BLOCK_LEN}  T_MAX={T_MAX}  N_SIM={N_SIM}  "
          f"thresholds: cashout {CASHOUT_PCT}% / daily {DAILY_LIMIT_PCT}% / totalDD {TOTAL_DD_PCT}%\n")

    SCENARIOS = [
        ("baseline (1.0bp cost)", ORB_BASE),
        ("stress (3.0bp cost)", ORB_BASE.with_(cost_bps_roundtrip=3.0)),
    ]

    rng = np.random.default_rng(SEED)
    all_rows = []
    for scen_label, cfg in SCENARIOS:
        r_series = build_daily_R(m1, cfg)
        n_trade_days = int((r_series != 0).sum())
        win_rate_R = float((r_series[r_series != 0] > 0).mean())
        avg_R = float(r_series[r_series != 0].mean())
        streak = longest_losing_streak(r_series)
        print(f"=== {scen_label} === n_valid_days={len(r_series)} n_trades={n_trade_days} "
              f"win_rate={win_rate_R:.3f} avg_R={avg_R:+.3f} longest_losing_streak={streak}")
        for risk_pct in RISK_PCT_GRID:
            stats = prop_sim(r_series, risk_pct, rng)
            print(f"  risk={risk_pct:4.2f}%/trade  " + fmt(stats, ""))
            stats.update(scenario=scen_label, risk_pct=risk_pct, n_trades=n_trade_days,
                         win_rate_R=win_rate_R, avg_R=avg_R, longest_losing_streak=streak)
            all_rows.append(stats)
        print()

    summary = pd.DataFrame(all_rows)
    out_csv = Path(__file__).resolve().parent.parent / "reports" / "s021_propscheme_summary.csv"
    out_csv.parent.mkdir(exist_ok=True)
    summary.to_csv(out_csv, index=False)
    print(f"Summary written to {out_csv}")
