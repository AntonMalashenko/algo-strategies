"""S021 stop variants -- Gate 3 risk grid for the leading stop variants (fair comparison: each
stop gets its own best risk %, since at a fixed %-risk a tighter stop is simply more leverage).
Companion to backtest/run_s021_stops.py (2026-09-28). Usage: python3 backtest/run_s021_stops_riskgrid.py
"""
import sys, numpy as np, pandas as pd
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from backtest.run_s021_stops import VARIANTS, run_variant
from backtest.run_s021_propscheme import prop_sim, DATA_DIR, SEED
from strategies.orb_intraday.engine import load_nsxusd_m1, compute_daily_sessions, compute_adr14
from strategies.orb_intraday.config import ORB_BASE
m1 = load_nsxusd_m1(DATA_DIR)
vd = compute_adr14(compute_daily_sessions(m1, ORB_BASE), ORB_BASE).dropna().index
pick = ["size 0.30", "size 0.40 (=opp band)", "size 0.50", "close-stop 0.50", "BASE size 0.75", "trail 0.50", "size 1.00"]
rows = []
for name, cfg in VARIANTS:
    if name not in pick: continue
    _, daily = run_variant(name, cfg, m1, vd)
    for risk in (0.25, 0.35, 0.50, 0.75, 1.00):
        st = prop_sim(daily, risk, np.random.default_rng(SEED)); st.update(variant=name, risk_pct=risk); rows.append(st)
        print(f"{name:22s} {risk:.2f}% cash={st['cashout_pct']:5.1f} dd={st['bust_dd_pct']:5.1f} to={st['timeout_pct']:5.1f} med={st['median_days_cashout']:.0f}", flush=True)
pd.DataFrame(rows).to_csv("reports/s021_stops_gate3_riskgrid.csv", index=False)
