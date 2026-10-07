"""Ad-hoc analysis (not a committed Gate 3 artifact): S021 FTMO free-trial
2-step pass probability + payout economics at RM 1%, requested by Anton
2026-10-02, comparing the IDEALIZED fixed-%-risk framework (same shape as
run_s021_propscheme.py) against the REAL economics currently forced on the
live ftmo-17211983 account by cTrader's 0.01 min-lot floor on US100.cash
(confirmed live 2026-09-29: money_per_point_per_lot=100 => $1/point at the
floor, independent of risk_pct for the vast majority of days -- see
bot/orb_signals.py::decide()'s lots_for_risk call and the skip_risk_cap
events observed live).

Real FTMO free-trial-2step numbers from scripts/seed_ftmo_account.py /
FTMO client-area screenshot (2026-09-29): profit target +$500 (5% of
$10,000), max daily loss -$500 (5%), max loss -$1000 (10%), min 2 trading
days. (Standard paid FTMO 2-Step Challenge is 10%/5% two-phase per
claude/research-2026-08-21-prop-firms-algo-trading.md -- this free trial's
single 5% target is NOT assumed identical to that; flagged in the output.)
"""
from __future__ import annotations
import sys
from pathlib import Path
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from strategies.orb_intraday.config import ORB_BASE
from strategies.orb_intraday.engine import (
    load_nsxusd_m1, compute_daily_sessions, compute_adr14, simulate, trades_to_frame,
)

DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "histdata"
BALANCE = 10000.0
MPPL = 100.0          # $ per point per 1.0 lot, US100.cash on FTMO cTrader (live-confirmed)
# Broker minimum in this codebase's lot units (ALGODEV-55, 2026-09-29): FTMO
# US100.cash minVolume = 0.01 contract = 0.0001 lot. The old 0.01-lot floor
# (= 1 contract) was the pre-fix fixed_lot guess, 100x too big.
MIN_LOT = 0.0001
LOT_STEP = 0.01

BLOCK_LEN, T_MAX, N_SIM, SEED = 5, 500, 8000, 20261002

# Real FTMO free-trial-2step numbers (seed_ftmo_account.py)
TARGET_PCT = 5.0
DAILY_LIMIT_PCT = -5.0
TOTAL_DD_PCT = 10.0
# Account's own tighter self-imposed backstop (bot/account_guard.py), checked separately
GUARD_DAILY_PCT = -4.5
GUARD_TOTAL_PCT = 8.0
MIN_TRADING_DAYS = 2

rng = np.random.default_rng(SEED)

m1 = load_nsxusd_m1(DATA_DIR)
daily = compute_daily_sessions(m1, ORB_BASE)
adr14 = compute_adr14(daily, ORB_BASE)
valid_days = adr14.dropna().index
trades = trades_to_frame(simulate(m1, ORB_BASE)).set_index("day")

stop_dist_by_day = {}
netpts_by_day = {}
for d in valid_days:
    stop_dist_by_day[d] = ORB_BASE.stop_adr_mult * adr14.loc[d]
    if d in trades.index:
        row = trades.loc[d]
        netpts_by_day[d] = float(row["net_pts"])
    else:
        netpts_by_day[d] = 0.0

stop_dist = np.array([stop_dist_by_day[d] for d in valid_days])
net_pts = np.array([netpts_by_day[d] for d in valid_days])
R = np.divide(net_pts, stop_dist, out=np.zeros_like(net_pts), where=stop_dist > 0)

print(f"n_valid_days={len(valid_days)} n_trade_days={(net_pts != 0).sum()} "
      f"win_rate={ (R[R!=0]>0).mean():.3f} avg_R={R[R!=0].mean():+.3f}")
print(f"stop_dist (pts): median={np.median(stop_dist):.1f} "
      f"p5={np.percentile(stop_dist,5):.1f} p25={np.percentile(stop_dist,25):.1f} "
      f"p75={np.percentile(stop_dist,75):.1f} p95={np.percentile(stop_dist,95):.1f}\n")


def lot_for_risk(risk_pct: float, sd: np.ndarray) -> np.ndarray:
    risk_amount = BALANCE * risk_pct / 100.0
    natural = risk_amount / (sd * MPPL)
    stepped = np.floor(natural / LOT_STEP) * LOT_STEP
    return np.maximum(stepped, MIN_LOT)


def day_pct_series(risk_pct: float) -> np.ndarray:
    """% of BALANCE this day's trade actually risks/returns, net_pts * lot * MPPL / BALANCE,
    with the SAME min-lot flooring bot/risk.py::lots_for_risk applies live."""
    lot = lot_for_risk(risk_pct, np.where(stop_dist > 0, stop_dist, 1.0))
    dollar_pnl = net_pts * lot * MPPL
    return 100.0 * dollar_pnl / BALANCE


def frac_floored(risk_pct: float) -> float:
    lot = lot_for_risk(risk_pct, np.where(stop_dist > 0, stop_dist, 1.0))
    trade_mask = net_pts != 0
    return float((lot[trade_mask] <= MIN_LOT + 1e-9).mean()) if trade_mask.any() else float("nan")


def prop_sim(day_pct: np.ndarray, target_pct: float, daily_limit_pct: float,
             total_dd_pct: float) -> dict:
    Dn = len(day_pct)
    n_blocks = int(np.ceil(T_MAX / BLOCK_LEN))
    starts = rng.integers(0, max(Dn - BLOCK_LEN, 1), size=(N_SIM, n_blocks))
    offsets = np.arange(BLOCK_LEN)
    idx = (starts[:, :, None] + offsets[None, None, :]).reshape(N_SIM, -1)[:, :T_MAX]
    idx = np.clip(idx, 0, Dn - 1)

    dp = day_pct[idx]
    cum = np.cumsum(dp, axis=1)
    peak = np.maximum.accumulate(cum, axis=1)
    dd = peak - cum

    daily_breach = dp <= daily_limit_pct
    dd_breach = dd >= total_dd_pct
    cash = cum >= target_pct
    stop = daily_breach | dd_breach | cash

    any_stop = stop.any(axis=1)
    first = np.where(any_stop, np.argmax(stop, axis=1), T_MAX - 1)
    rows = np.arange(N_SIM)
    outcome = np.full(N_SIM, "timeout", dtype=object)
    is_daily = daily_breach[rows, first] & any_stop
    is_dd = dd_breach[rows, first] & any_stop & ~is_daily
    is_cash = cash[rows, first] & any_stop & ~is_daily & ~is_dd
    outcome[is_daily] = "bust_daily"
    outcome[is_dd] = "bust_dd"
    outcome[is_cash] = "cashout"
    days = first + 1

    too_fast = (outcome == "cashout") & (days < MIN_TRADING_DAYS)
    return dict(
        cashout_pct=100 * float((outcome == "cashout").mean()),
        bust_daily_pct=100 * float((outcome == "bust_daily").mean()),
        bust_dd_pct=100 * float((outcome == "bust_dd").mean()),
        timeout_pct=100 * float((outcome == "timeout").mean()),
        median_days_cashout=(float(np.median(days[outcome == "cashout"]))
                              if (outcome == "cashout").any() else float("nan")),
        pct_cashout_under_min_days=100 * float(too_fast.sum()) / max((outcome == "cashout").sum(), 1),
        worst_day_pct=float(dp.min()),
        p5_day_pct=float(np.percentile(dp, 5)),
    )


def report(label, risk_pct, target_pct, daily_limit_pct, total_dd_pct):
    dp = day_pct_series(risk_pct)
    ff = frac_floored(risk_pct)
    stats = prop_sim(dp, target_pct, daily_limit_pct, total_dd_pct)
    print(f"--- {label} (nominal RM={risk_pct}%, rules target={target_pct}% "
          f"daily={daily_limit_pct}% totalDD={total_dd_pct}%) ---")
    print(f"  trade-days floored to min lot (0.01): {100*ff:.1f}%")
    print(f"  cashout {stats['cashout_pct']:.1f}%  bust-daily {stats['bust_daily_pct']:.1f}%  "
          f"bust-dd {stats['bust_dd_pct']:.1f}%  timeout {stats['timeout_pct']:.1f}%")
    print(f"  median days to cashout: {stats['median_days_cashout']:.0f}  "
          f"(cashouts under {MIN_TRADING_DAYS}-day min: {stats['pct_cashout_under_min_days']:.1f}%)")
    print(f"  worst single day: {stats['worst_day_pct']:+.2f}%   p5 day: {stats['p5_day_pct']:+.2f}%")
    for split in (0.80, 0.90):
        ev = stats['cashout_pct']/100 * (target_pct/100 * BALANCE) * split
        print(f"  EV of payout at {split:.0%} split (unconditional, phase-1 only): ${ev:,.1f}")
    print()
    return stats


print("### A) IDEALIZED fixed-%-risk framework (same shape as run_s021_propscheme.py), "
      "for comparability with the passport's existing grid ###")
report("idealized RM=1.0%, generic +9/-3/-10 thresholds (existing passport grid point)",
       1.00, 9.0, -3.0, 10.0)
report("idealized RM=1.0%, REAL FTMO free-trial rules", 1.00, TARGET_PCT, DAILY_LIMIT_PCT, TOTAL_DD_PCT)

print("### B) REAL economics on the live $10k FTMO account (0.01-lot floor applied, "
      "as bot/risk.py::lots_for_risk actually sizes it) ###")
report("configured RM=0.50% (current live setting)", 0.50, TARGET_PCT, DAILY_LIMIT_PCT, TOTAL_DD_PCT)
report("configured RM=1.00% (what Anton is asking about)", 1.00, TARGET_PCT, DAILY_LIMIT_PCT, TOTAL_DD_PCT)
report("configured RM=1.00%, against the ACCOUNT'S OWN tighter guard (-4.5%/-8%)",
       1.00, TARGET_PCT, GUARD_DAILY_PCT, GUARD_TOTAL_PCT)

print("### C) At what nominal RM would the floor stop dominating (sanity check) ###")
for rp in (1.0, 2.0, 3.0, 5.0, 8.0):
    print(f"  RM={rp:4.1f}%: floored fraction of trade-days = {100*frac_floored(rp):.1f}%")

print("\n### A-corrected) TRUE idealized fixed-%-risk (R*risk_pct, no lot flooring at all --"
      " matches run_s021_propscheme.py's own math exactly) ###")

def day_pct_idealized(risk_pct: float) -> np.ndarray:
    return R * risk_pct

def report_idealized(label, risk_pct, target_pct, daily_limit_pct, total_dd_pct):
    dp = day_pct_idealized(risk_pct)
    stats = prop_sim(dp, target_pct, daily_limit_pct, total_dd_pct)
    print(f"--- {label} ---")
    print(f"  cashout {stats['cashout_pct']:.1f}%  bust-daily {stats['bust_daily_pct']:.1f}%  "
          f"bust-dd {stats['bust_dd_pct']:.1f}%  timeout {stats['timeout_pct']:.1f}%  "
          f"median days {stats['median_days_cashout']:.0f}")

report_idealized("RM=1.0%, generic +9/-3/-10 (sanity check vs passport's published grid)",
                  1.00, 9.0, -3.0, 10.0)
report_idealized("RM=1.0%, REAL FTMO free-trial rules (5/-5/-10)", 1.00, TARGET_PCT, DAILY_LIMIT_PCT, TOTAL_DD_PCT)
