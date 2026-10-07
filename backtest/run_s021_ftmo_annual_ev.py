"""Ad-hoc analysis (not committed Gate 3), requested by Anton 2026-10-02:
annual expected value of running S021 ALONE on a $10,000 FTMO account,
repeatedly re-buying the standard PAID 2-Step Challenge (10%/5% phase
targets, 5% daily loss, 10% max loss, $70 fee/attempt) every time an
attempt or a funded account is busted, across a risk-per-trade grid
0.25%-2.00%.

Uses the SAME real-floored-lot economics established 2026-10-02
(US100.cash min lot 0.01, $100/point/lot on this FTMO account -- so for
most of this RM grid the broker's min-lot floor, not the configured
risk_pct, is what actually sizes the trade; see
backtest/run_s021_ftmo_rm_analysis.py for that derivation) over the real
S021 day-R/day-pts series from the independent engine
(strategies/orb_intraday), block-bootstrapped continuously for a
252-trading-day year per simulated path -- phase/funded-account resets
happen WITHIN that one continuous market sequence (the market doesn't
restart when the account does).

Funded-stage assumptions (NOT separately confirmed against FTMO's live
funded-account Terms -- flagged in the output): same 5%/10% daily/max-loss
discipline continues; payout every ~10 trading days (approximating FTMO's
real 14-calendar-day cycle); split 80% by default; a funded-account bust
forfeits only the UN-paid-out accrued profit since the last payout cycle
and forces a fresh paid Challenge attempt.
"""
from __future__ import annotations
import sys
from pathlib import Path
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from strategies.orb_intraday.config import ORB_BASE
from strategies.orb_intraday.engine import (
    load_nsxusd_m1, compute_daily_sessions, compute_adr14, simulate, trades_to_frame,
)

DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "histdata"
BALANCE = 10000.0
MPPL = 100.0
# Broker minimum in this codebase's lot units (ALGODEV-55, 2026-09-29): FTMO
# US100.cash minVolume = 0.01 contract = 0.0001 lot. The old 0.01-lot floor
# (= 1 contract) was the pre-fix fixed_lot guess, 100x too big.
MIN_LOT = 0.0001
PHASE1_TARGET, PHASE2_TARGET = 10.0, 5.0
DAILY_LIMIT_PCT, TOTAL_DD_PCT = -5.0, 10.0
CHALLENGE_FEE = 70.0
PAYOUT_CYCLE_DAYS = 10          # ~2 real-world weeks of trading days
PAYOUT_SPLIT = 0.80
TRADING_DAYS_PER_YEAR = 252

BLOCK_LEN, N_SIM, SEED = 5, 6000, 20261002
RM_GRID = [0.25, 0.50, 0.75, 1.00, 1.25, 1.50, 1.75, 2.00]

rng = np.random.default_rng(SEED)

m1 = load_nsxusd_m1(DATA_DIR)
daily = compute_daily_sessions(m1, ORB_BASE)
adr14 = compute_adr14(daily, ORB_BASE)
valid_days = adr14.dropna().index
trades = trades_to_frame(simulate(m1, ORB_BASE)).set_index("day")
stop_dist = np.array([ORB_BASE.stop_adr_mult * adr14.loc[d] for d in valid_days])
net_pts = np.array([float(trades.loc[d, "net_pts"]) if d in trades.index else 0.0
                     for d in valid_days])
Dn = len(valid_days)


def day_pct_series(risk_pct: float) -> np.ndarray:
    risk_amount = BALANCE * risk_pct / 100.0
    natural = risk_amount / (stop_dist * MPPL)
    lot = np.maximum(natural, MIN_LOT)
    return 100.0 * (net_pts * lot * MPPL) / BALANCE


def bootstrap_paths(day_pct: np.ndarray, n_days: int, n_sim: int) -> np.ndarray:
    n_blocks = int(np.ceil(n_days / BLOCK_LEN))
    starts = rng.integers(0, max(Dn - BLOCK_LEN, 1), size=(n_sim, n_blocks))
    offsets = np.arange(BLOCK_LEN)
    idx = (starts[:, :, None] + offsets[None, None, :]).reshape(n_sim, -1)[:, :n_days]
    idx = np.clip(idx, 0, Dn - 1)
    return day_pct[idx]


def simulate_year(risk_pct: float) -> dict:
    dp = day_pct_series(risk_pct)
    paths = bootstrap_paths(dp, TRADING_DAYS_PER_YEAR, N_SIM)   # (N_SIM, 252)

    phase = np.ones(N_SIM, dtype=int)          # 1, 2, or 3=funded
    equity_rel = np.zeros(N_SIM)                 # % within current phase/funded window
    peak = np.zeros(N_SIM)
    last_payout_equity = np.zeros(N_SIM)
    days_since_payout = np.zeros(N_SIM, dtype=int)
    attempts = np.ones(N_SIM, dtype=int)          # first purchase already counted
    fees_paid = np.full(N_SIM, CHALLENGE_FEE)
    payouts_received = np.zeros(N_SIM)
    funded_busts = np.zeros(N_SIM, dtype=int)
    max_dd_seen = np.zeros(N_SIM)
    funded_days = np.zeros(N_SIM, dtype=int)

    for day in range(TRADING_DAYS_PER_YEAR):
        today = paths[:, day]
        equity_rel = equity_rel + today
        peak = np.maximum(peak, equity_rel)
        dd = peak - equity_rel
        max_dd_seen = np.maximum(max_dd_seen, dd)

        daily_breach = today <= DAILY_LIMIT_PCT
        dd_breach = dd >= TOTAL_DD_PCT
        busted = daily_breach | dd_breach

        target = np.where(phase == 1, PHASE1_TARGET, PHASE2_TARGET)
        passed = (equity_rel >= target) & (phase != 3) & ~busted

        funded_mask = (phase == 3)
        funded_busted = busted & funded_mask
        funded_busts += funded_busted.astype(int)

        # funded payout cycle (checked for funded paths not busted today)
        funded_days += funded_mask.astype(int)
        days_since_payout = np.where(funded_mask, days_since_payout + 1, days_since_payout)
        do_payout = funded_mask & (days_since_payout >= PAYOUT_CYCLE_DAYS) & ~busted
        increment = np.maximum(0.0, equity_rel - last_payout_equity)
        payouts_received = np.where(do_payout, payouts_received + increment * PAYOUT_SPLIT / 100.0 * BALANCE,
                                     payouts_received)
        last_payout_equity = np.where(do_payout, equity_rel, last_payout_equity)
        days_since_payout = np.where(do_payout, 0, days_since_payout)

        # resets on bust (any phase) -> back to a freshly purchased phase 1
        reset_mask = busted
        attempts += reset_mask.astype(int)
        fees_paid = np.where(reset_mask, fees_paid + CHALLENGE_FEE, fees_paid)
        phase = np.where(reset_mask, 1, phase)
        equity_rel = np.where(reset_mask, 0.0, equity_rel)
        peak = np.where(reset_mask, 0.0, peak)
        last_payout_equity = np.where(reset_mask, 0.0, last_payout_equity)
        days_since_payout = np.where(reset_mask, 0, days_since_payout)

        # phase transitions on pass (not busted)
        p1_to_p2 = passed & (phase == 1)
        p2_to_funded = passed & (phase == 2)
        phase = np.where(p1_to_p2, 2, phase)
        phase = np.where(p2_to_funded, 3, phase)
        equity_rel = np.where(passed, 0.0, equity_rel)
        peak = np.where(passed, 0.0, peak)
        last_payout_equity = np.where(p2_to_funded, 0.0, last_payout_equity)
        days_since_payout = np.where(p2_to_funded, 0, days_since_payout)

    net_profit = payouts_received - fees_paid
    return dict(
        risk_pct=risk_pct,
        mean_net=float(net_profit.mean()), median_net=float(np.median(net_profit)),
        p5_net=float(np.percentile(net_profit, 5)), p95_net=float(np.percentile(net_profit, 95)),
        prob_funded_eoy=100 * float((phase == 3).mean()),
        prob_ever_funded=100 * float(((phase == 3) | (funded_busts > 0)).mean()),
        prob_funded_bust=100 * float((funded_busts > 0).mean()),
        mean_attempts=float(attempts.mean()),
        mean_funded_days=float(funded_days.mean()),
        mean_max_dd=float(max_dd_seen.mean()), p95_max_dd=float(np.percentile(max_dd_seen, 95)),
        mean_fees=float(fees_paid.mean()), mean_payouts=float(payouts_received.mean()),
    )


print(f"S021 alone, $10k FTMO 2-Step (10%/5%, -5%/-10%), ${CHALLENGE_FEE:.0f}/attempt, "
      f"{PAYOUT_SPLIT:.0%} split, payout every {PAYOUT_CYCLE_DAYS} trading days, "
      f"{TRADING_DAYS_PER_YEAR} trading days/year, N_SIM={N_SIM}\n")
rows = []
for rp in RM_GRID:
    r = simulate_year(rp)
    rows.append(r)
    print(f"RM {rp:4.2f}%  net/yr mean=${r['mean_net']:+7.0f} median=${r['median_net']:+7.0f} "
          f"p5/p95=[${r['p5_net']:+.0f},${r['p95_net']:+.0f}]  "
          f"funded@EOY={r['prob_funded_eoy']:4.1f}%  ever-funded={r['prob_ever_funded']:4.1f}%  "
          f"funded-bust>=1={r['prob_funded_bust']:4.1f}%  attempts/yr={r['mean_attempts']:.2f}  "
          f"maxDD avg/p95={r['mean_max_dd']:.1f}/{r['p95_max_dd']:.1f}%  "
          f"fees=${r['mean_fees']:.0f} payouts=${r['mean_payouts']:.0f}")
