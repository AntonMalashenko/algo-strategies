"""Ad-hoc "emulator" run (not committed Gate 3), requested by Anton
2026-10-02: literal day-by-day simulation of repeatedly buying/busting the
FTMO 2-Step Challenge (10%/5%, -5%/-10%, $70/attempt) on a $10,000 account
running S021 alone, over a multi-year horizon, both as an aggregate
cumulative-profit curve (percentile bands across many simulated accounts)
and as one concrete example path's event log (every bust/pass/payout),
so the "all the busts and challenges" mechanics are visible, not just a
one-year summary table (see run_s021_ftmo_annual_ev.py, same day).

Same real-floored-lot economics and state machine as
run_s021_ftmo_annual_ev.py; only the reporting changes (multi-year,
per-day cumulative tracking, one example path logged in full).
"""
from __future__ import annotations
import sys
from pathlib import Path
import numpy as np
import json

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
PAYOUT_CYCLE_DAYS = 10
PAYOUT_SPLIT = 0.80
# FTMO 2-Step max loss is STATIC: equity may not fall below initial balance
# -10% (FTMO rules page, see scripts/seed_ftmo_account.py). A payout
# withdraws the profit and resets the balance to initial, so the floor is
# always 10% below the last payout level (0 = initial). False = the older
# trailing-from-peak rule these ad-hoc scripts used before 2026-10-02.
MAX_LOSS_STATIC = True
YEARS = 3
TRADING_DAYS = 252 * YEARS

BLOCK_LEN, N_SIM, SEED = 5, 6000, 20261002
RM_SET = [0.50, 1.00, 1.50, 2.00, 2.50, 3.00, 3.50, 4.00, 4.50, 5.00]

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


def simulate_multiyear(risk_pct: float, log_path0: bool = False):
    dp = day_pct_series(risk_pct)
    paths = bootstrap_paths(dp, TRADING_DAYS, N_SIM)

    phase = np.ones(N_SIM, dtype=int)
    equity_rel = np.zeros(N_SIM)
    peak = np.zeros(N_SIM)
    last_payout_equity = np.zeros(N_SIM)
    days_since_payout = np.zeros(N_SIM, dtype=int)
    fees_paid = np.full(N_SIM, CHALLENGE_FEE)
    payouts_received = np.zeros(N_SIM)

    cum_net_by_day = np.zeros((TRADING_DAYS, N_SIM), dtype=np.float32)
    events = [] if log_path0 else None
    if log_path0:
        events.append(dict(day=0, kind="start_attempt", phase=1))

    for day in range(TRADING_DAYS):
        today = paths[:, day]
        equity_rel = equity_rel + today
        peak = np.maximum(peak, equity_rel)
        dd = peak - equity_rel

        daily_breach = today <= DAILY_LIMIT_PCT
        if MAX_LOSS_STATIC:
            dd_breach = (last_payout_equity - equity_rel) >= TOTAL_DD_PCT
        else:
            dd_breach = dd >= TOTAL_DD_PCT
        busted = daily_breach | dd_breach

        target = np.where(phase == 1, PHASE1_TARGET, PHASE2_TARGET)
        passed = (equity_rel >= target) & (phase != 3) & ~busted

        funded_mask = (phase == 3)
        days_since_payout = np.where(funded_mask, days_since_payout + 1, days_since_payout)
        # a payout date with no profit above the last payout level pays nothing and
        # must NOT lower that level (balance below initial is not reset upward)
        do_payout = (funded_mask & (days_since_payout >= PAYOUT_CYCLE_DAYS) & ~busted
                     & (equity_rel > last_payout_equity))
        increment = np.maximum(0.0, equity_rel - last_payout_equity)
        pay_amt = increment * PAYOUT_SPLIT / 100.0 * BALANCE
        payouts_received = np.where(do_payout, payouts_received + pay_amt, payouts_received)
        last_payout_equity = np.where(do_payout, equity_rel, last_payout_equity)
        days_since_payout = np.where(do_payout, 0, days_since_payout)

        if log_path0:
            p0_phase_before = phase[0]
            if busted[0]:
                reason = "daily -5%" if daily_breach[0] else "total DD -10%"
                was = "funded account" if p0_phase_before == 3 else f"phase {p0_phase_before}"
                events.append(dict(day=day, kind="bust", what=was, reason=reason,
                                    equity_pct=round(float(equity_rel[0]), 2)))
            elif do_payout[0] and pay_amt[0] > 0:
                events.append(dict(day=day, kind="payout", amount=round(float(pay_amt[0]), 1),
                                    cum_payouts=round(float(payouts_received[0]), 1)))
            if passed[0]:
                if p0_phase_before == 1:
                    events.append(dict(day=day, kind="pass_phase1", equity_pct=round(float(equity_rel[0]), 2)))
                elif p0_phase_before == 2:
                    events.append(dict(day=day, kind="pass_phase2_FUNDED", equity_pct=round(float(equity_rel[0]), 2)))

        reset_mask = busted
        fees_paid = np.where(reset_mask, fees_paid + CHALLENGE_FEE, fees_paid)
        phase = np.where(reset_mask, 1, phase)
        equity_rel = np.where(reset_mask, 0.0, equity_rel)
        peak = np.where(reset_mask, 0.0, peak)
        last_payout_equity = np.where(reset_mask, 0.0, last_payout_equity)
        days_since_payout = np.where(reset_mask, 0, days_since_payout)

        if log_path0 and busted[0]:
            events.append(dict(day=day, kind="start_attempt", phase=1,
                                fees_so_far=round(float(fees_paid[0]), 0)))

        p1_to_p2 = passed & (phase == 1)
        p2_to_funded = passed & (phase == 2)
        phase = np.where(p1_to_p2, 2, phase)
        phase = np.where(p2_to_funded, 3, phase)
        equity_rel = np.where(passed, 0.0, equity_rel)
        peak = np.where(passed, 0.0, peak)
        last_payout_equity = np.where(p2_to_funded, 0.0, last_payout_equity)
        days_since_payout = np.where(p2_to_funded, 0, days_since_payout)

        cum_net_by_day[day] = payouts_received - fees_paid

    return cum_net_by_day, events, fees_paid


print(f"S021 alone, ${BALANCE:.0f} FTMO 2-Step, ${CHALLENGE_FEE:.0f}/attempt, {YEARS} years "
      f"({TRADING_DAYS} trading days), N_SIM={N_SIM}\n")

summary = {}
events_by_rm = {}
stats = {}
print(f"data: {valid_days[0].date()} .. {valid_days[-1].date()}  ({Dn} sessions)")
for rm in RM_SET:
    cum, ev, fees = simulate_multiyear(rm, log_path0=True)
    stats[rm] = dict(busts_mean=float(np.mean(fees / CHALLENGE_FEE - 1)),
                     p_loss=float(np.mean(cum[-1] < 0)),
                     **{f"y{yr}_{k}": float(f(cum[min(yr * 252, TRADING_DAYS) - 1]))
                        for yr in (1, 2, 3)
                        for k, f in (("median", np.median), ("mean", np.mean),
                                     ("p5", lambda r: np.percentile(r, 5)),
                                     ("p95", lambda r: np.percentile(r, 95)))})
    summary[rm] = cum
    events_by_rm[rm] = ev
    for yr in (1, 2, 3):
        d = min(yr * 252, TRADING_DAYS) - 1
        row = cum[d]
        print(f"RM {rm:.2f}%  year {yr}: median=${np.median(row):+.0f}  "
              f"mean=${row.mean():+.0f}  p5=${np.percentile(row,5):+.0f}  "
              f"p25=${np.percentile(row,25):+.0f}  p75=${np.percentile(row,75):+.0f}  "
              f"p95=${np.percentile(row,95):+.0f}")
    print()

# dump percentile bands for charting
out = {"days": list(range(TRADING_DAYS))}
for rm in RM_SET:
    cum = summary[rm]
    out[f"rm{rm}_p5"] = np.percentile(cum, 5, axis=1).round(1).tolist()
    out[f"rm{rm}_p25"] = np.percentile(cum, 25, axis=1).round(1).tolist()
    out[f"rm{rm}_median"] = np.percentile(cum, 50, axis=1).round(1).tolist()
    out[f"rm{rm}_p75"] = np.percentile(cum, 75, axis=1).round(1).tolist()
    out[f"rm{rm}_p95"] = np.percentile(cum, 95, axis=1).round(1).tolist()
    # 6 example individual paths for texture
    out[f"rm{rm}_samples"] = cum[:, :6].round(1).T.tolist()

with open(Path(__file__).resolve().parent.parent / "reports" / "s021_ftmo_emulator_bands.json", "w") as f:
    json.dump(out, f)

with open(Path(__file__).resolve().parent.parent / "reports" / "s021_ftmo_emulator_stats.json", "w") as f:
    json.dump({f"{rm:.2f}": v for rm, v in stats.items()}, f)

events_json = {f"{rm:.2f}": ev for rm, ev in events_by_rm.items()}
with open(Path(__file__).resolve().parent.parent / "reports" / "s021_ftmo_emulator_events.json", "w") as f:
    json.dump(events_json, f)

for rm in RM_SET:
    ev = events_by_rm[rm]
    print(f"--- example single-account path log (RM={rm:.2f}%, path #0) --- {len(ev)} events")
    for e in ev[:15]:
        print(e)
    print("...")
    print()
