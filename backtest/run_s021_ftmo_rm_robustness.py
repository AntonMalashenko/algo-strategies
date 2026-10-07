"""Ad-hoc (2026-10-02, Anton): where is the optimal RM for S021 on a $10K FTMO
2-Step when the backtest edge is weaker than measured and stop-outs slip?
Same emulator state machine/economics as run_s021_ftmo_bust_stats.py (real
sizing, ALGODEV-55); only the day-level P&L series is stressed:
  edge haircut h : every traded day loses h * (mean net_pts per trade), so the
                   average trade shrinks by h (h=0.5 -> half the edge)
  stop slippage s: days that closed at the stop lose (1+s) x the stop distance
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
BALANCE, MPPL, MIN_LOT = 10000.0, 100.0, 0.0001   # ALGODEV-55: FTMO min = 0.01 contract
PHASE1_TARGET, PHASE2_TARGET = 10.0, 5.0
DAILY_LIMIT_PCT, TOTAL_DD_PCT = -5.0, 10.0
CHALLENGE_FEE, PAYOUT_CYCLE_DAYS, PAYOUT_SPLIT = 70.0, 10, 0.80
TRADING_DAYS, BLOCK_LEN, N_SIM, SEED = 252 * 3, 5, 6000, 20261002
# FTMO 2-Step max loss is STATIC: equity may not fall below initial balance
# -10% (FTMO rules page, see scripts/seed_ftmo_account.py). A payout
# withdraws the profit and resets the balance to initial, so the floor is
# always 10% below the last payout level (0 = initial). False = the older
# trailing-from-peak rule these ad-hoc scripts used before 2026-10-02.
MAX_LOSS_STATIC = True
STOP_HIT_FRAC = 0.95
RM_SET = [0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 3.5, 4.0, 4.5]
SCENARIOS = [("base", 0.0, 0.0), ("edge -25%", 0.25, 0.0), ("edge -50%", 0.50, 0.0),
             ("slip 10%", 0.0, 0.10), ("edge -25% + slip 10%", 0.25, 0.10),
             ("edge -50% + slip 10%", 0.50, 0.10)]

m1 = load_nsxusd_m1(DATA_DIR)
daily = compute_daily_sessions(m1, ORB_BASE)
adr14 = compute_adr14(daily, ORB_BASE)
valid_days = adr14.dropna().index
trades = trades_to_frame(simulate(m1, ORB_BASE)).set_index("day")
stop_dist = np.array([ORB_BASE.stop_adr_mult * adr14.loc[d] for d in valid_days])
traded = np.array([d in trades.index for d in valid_days])
net_pts0 = np.array([float(trades.loc[d, "net_pts"]) if t else 0.0 for d, t in zip(valid_days, traded)])
Dn = len(valid_days)
R0 = net_pts0[traded] / stop_dist[traded]
print(f"trades={traded.sum()}  mean R/trade={R0.mean():+.3f}  win={np.mean(R0>0):.1%}  stop-hit days={np.mean(R0<=-STOP_HIT_FRAC):.1%}")


def series(h, s):
    pts = net_pts0.copy()
    stop_hit = traded & (pts <= -STOP_HIT_FRAC * stop_dist)
    pts[stop_hit] = pts[stop_hit] * (1 + s)
    pts[traded] -= h * net_pts0[traded].mean()
    return pts


def run(pts, risk_pct, rng):
    lot = np.maximum(BALANCE * risk_pct / 100.0 / (stop_dist * MPPL), MIN_LOT)
    dp = 100.0 * pts * lot * MPPL / BALANCE
    nb = int(np.ceil(TRADING_DAYS / BLOCK_LEN))
    st = rng.integers(0, Dn - BLOCK_LEN, size=(N_SIM, nb))
    idx = (st[:, :, None] + np.arange(BLOCK_LEN)[None, None, :]).reshape(N_SIM, -1)[:, :TRADING_DAYS]
    paths = dp[idx]
    ph = np.ones(N_SIM, int); eq = np.zeros(N_SIM); pk = np.zeros(N_SIM); lpe = np.zeros(N_SIM)
    dsp = np.zeros(N_SIM, int); att = np.ones(N_SIM, int); pay = np.zeros(N_SIM)
    for day in range(TRADING_DAYS):
        t = paths[:, day]; eq = eq + t; pk = np.maximum(pk, eq)
        b = (t <= DAILY_LIMIT_PCT) | (((lpe - eq) if MAX_LOSS_STATIC else (pk - eq)) >= TOTAL_DD_PCT)
        tgt = np.where(ph == 1, PHASE1_TARGET, PHASE2_TARGET)
        ps = (eq >= tgt) & (ph != 3) & ~b
        f = ph == 3; dsp = np.where(f, dsp + 1, dsp)
        p = f & (dsp >= PAYOUT_CYCLE_DAYS) & ~b & (eq > lpe)
        pay = np.where(p, pay + np.maximum(0, eq - lpe) * PAYOUT_SPLIT / 100 * BALANCE, pay)
        lpe = np.where(p, eq, lpe); dsp = np.where(p, 0, dsp)
        att += b
        ph = np.where(b, 1, ph); eq = np.where(b, 0, eq); pk = np.where(b, 0, pk)
        lpe = np.where(b, 0, lpe); dsp = np.where(b, 0, dsp)
        tf = ps & (ph == 2)
        ph = np.where(ps & (ph == 1), 2, np.where(tf, 3, ph))
        eq = np.where(ps, 0, eq); pk = np.where(ps, 0, pk); lpe = np.where(tf, 0, lpe); dsp = np.where(tf, 0, dsp)
    net = pay - att * CHALLENGE_FEE
    return np.median(net), np.mean(att - 1), np.mean(net < 0)


for name, h, s in SCENARIOS:
    pts = series(h, s)
    rng = np.random.default_rng(SEED)
    rows = [(rm, *run(pts, rm, rng)) for rm in RM_SET]
    best = max(rows, key=lambda r: r[1])
    print(f"\n[{name}]  best median at RM {best[0]}%")
    print("  " + "  ".join(f"{rm:.1f}%:{med:+6.0f}/{bu:4.1f}/{pl:4.0%}" for rm, med, bu, pl in rows))
