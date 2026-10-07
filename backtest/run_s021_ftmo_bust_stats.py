"""Ad-hoc (2026-10-02, Anton): aggregate bust/attempt statistics per RM for the
FTMO 2-Step emulator (same state machine as run_s021_ftmo_emulator.py), so the
"which RM busts least / earns most" question is answered across all simulated
accounts, not from one example path.

Two sizing modes:
  continuous - lot = max(natural, 0.01), no volume-step rounding (what the
               emulator table uses)
  stepped    - lot rounded to the nearest broker volume step (0.0001 lot),
               as CTraderS007._volume_from_lots does
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
# Broker minimum in this codebase's lot units (ALGODEV-55, 2026-09-29): FTMO
# US100.cash minVolume = 0.01 contract = 0.0001 lot. The old 0.01-lot floor
# (= 1 contract) was the pre-fix fixed_lot guess, 100x too big.
BALANCE, MPPL, MIN_LOT, LOT_STEP = 10000.0, 100.0, 0.0001, 0.0001
PHASE1_TARGET, PHASE2_TARGET = 10.0, 5.0
DAILY_LIMIT_PCT, TOTAL_DD_PCT = -5.0, 10.0
CHALLENGE_FEE, PAYOUT_CYCLE_DAYS, PAYOUT_SPLIT = 70.0, 10, 0.80
# FTMO 2-Step max loss is STATIC: equity may not fall below initial balance
# -10% (FTMO rules page, see scripts/seed_ftmo_account.py). A payout
# withdraws the profit and resets the balance to initial, so the floor is
# always 10% below the last payout level (0 = initial). False = the older
# trailing-from-peak rule these ad-hoc scripts used before 2026-10-02.
MAX_LOSS_STATIC = True
TRADING_DAYS = 252 * 3
BLOCK_LEN, N_SIM, SEED = 5, 6000, 20261002
RM_SET = [0.50, 1.00, 1.50, 2.00, 2.50, 3.00, 4.00, 5.00]
MODE = sys.argv[1] if len(sys.argv) > 1 else "continuous"
if len(sys.argv) > 2:
    RM_SET = [float(x) for x in sys.argv[2].split(",")]

m1 = load_nsxusd_m1(DATA_DIR)
daily = compute_daily_sessions(m1, ORB_BASE)
adr14 = compute_adr14(daily, ORB_BASE)
valid_days = adr14.dropna().index
trades = trades_to_frame(simulate(m1, ORB_BASE)).set_index("day")
stop_dist = np.array([ORB_BASE.stop_adr_mult * adr14.loc[d] for d in valid_days])
net_pts = np.array([float(trades.loc[d, "net_pts"]) if d in trades.index else 0.0 for d in valid_days])
Dn = len(valid_days)


def lots(risk_pct):
    natural = BALANCE * risk_pct / 100.0 / (stop_dist * MPPL)
    if MODE == "stepped":
        natural = np.round(natural / LOT_STEP) * LOT_STEP  # _volume_from_lots rounds to nearest step
    return np.maximum(natural, MIN_LOT)


def run(risk_pct, rng):
    lot = lots(risk_pct)
    dp = 100.0 * net_pts * lot * MPPL / BALANCE
    real_risk = 100.0 * lot * stop_dist * MPPL / BALANCE
    n_blocks = int(np.ceil(TRADING_DAYS / BLOCK_LEN))
    starts = rng.integers(0, Dn - BLOCK_LEN, size=(N_SIM, n_blocks))
    idx = (starts[:, :, None] + np.arange(BLOCK_LEN)[None, None, :]).reshape(N_SIM, -1)[:, :TRADING_DAYS]
    paths = dp[np.clip(idx, 0, Dn - 1)]

    phase = np.ones(N_SIM, int); eq = np.zeros(N_SIM); peak = np.zeros(N_SIM)
    lpe = np.zeros(N_SIM); dsp = np.zeros(N_SIM, int)
    attempts = np.ones(N_SIM, int); funded_busts = np.zeros(N_SIM, int)
    payouts = np.zeros(N_SIM); fundings = np.zeros(N_SIM, int)
    for day in range(TRADING_DAYS):
        t = paths[:, day]
        eq = eq + t; peak = np.maximum(peak, eq)
        floor_hit = ((lpe - eq) if MAX_LOSS_STATIC else (peak - eq)) >= TOTAL_DD_PCT
        busted = (t <= DAILY_LIMIT_PCT) | floor_hit
        target = np.where(phase == 1, PHASE1_TARGET, PHASE2_TARGET)
        passed = (eq >= target) & (phase != 3) & ~busted
        f = phase == 3
        dsp = np.where(f, dsp + 1, dsp)
        pay = f & (dsp >= PAYOUT_CYCLE_DAYS) & ~busted & (eq > lpe)
        amt = np.maximum(0.0, eq - lpe) * PAYOUT_SPLIT / 100.0 * BALANCE
        payouts = np.where(pay, payouts + amt, payouts)
        lpe = np.where(pay, eq, lpe); dsp = np.where(pay, 0, dsp)
        funded_busts += busted & f
        attempts += busted
        phase = np.where(busted, 1, phase); eq = np.where(busted, 0.0, eq)
        peak = np.where(busted, 0.0, peak); lpe = np.where(busted, 0.0, lpe); dsp = np.where(busted, 0, dsp)
        to_f = passed & (phase == 2)
        fundings += to_f
        phase = np.where(passed & (phase == 1), 2, np.where(to_f, 3, phase))
        eq = np.where(passed, 0.0, eq); peak = np.where(passed, 0.0, peak)
        lpe = np.where(to_f, 0.0, lpe); dsp = np.where(to_f, 0, dsp)
    net = payouts - attempts * CHALLENGE_FEE
    return dict(real_risk_med=np.median(real_risk), floored=np.mean(lot <= MIN_LOT + 1e-12),
                busts=np.mean(attempts - 1), busts_med=np.median(attempts - 1),
                fbusts=np.mean(funded_busts), fundings=np.mean(fundings),
                net_med=np.median(net), net_mean=net.mean(), net_p5=np.percentile(net, 5),
                p_loss=np.mean(net < 0), per_bust=np.median(net) / max(np.mean(attempts - 1), 1e-9))


rng = np.random.default_rng(SEED)
print(f"mode={MODE}  3 years, N_SIM={N_SIM}")
print(f"{'RM':>5} {'realRisk':>8} {'floor%':>6} {'busts':>6} {'fBusts':>6} {'funds':>6} {'netMed':>8} {'netMean':>8} {'netP5':>7} {'P(loss)':>7}")
for rm in RM_SET:
    r = run(rm, rng)
    print(f"{rm:>5.2f} {r['real_risk_med']:>7.2f}% {100*r['floored']:>5.0f}% {r['busts']:>6.1f} {r['fbusts']:>6.2f} "
          f"{r['fundings']:>6.2f} {r['net_med']:>8.0f} {r['net_mean']:>8.0f} {r['net_p5']:>7.0f} {100*r['p_loss']:>6.1f}%")
