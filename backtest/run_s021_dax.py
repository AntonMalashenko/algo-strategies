"""S021.2 -- frozen ORB_BASE rules on DAX (ORB_DAX), Gate 1 + FTMO prop emulator.

Requested 2026-10-06 (Anton): "давай докачаем, дополним 021 даксом и проверь на
симуляторе пропа ... при разных рисках на сделку 0.5/1/1.5/2/2.5/3/4/4.5".

Step `gate1`  : simulate(ORB_DAX) on Dukascopy GER40 2015-01..2026-10 (data/GER40/duka_long),
                per-year R, split into the window already looked at on 2026-10-06
                (2023-06-26..2026-08-11, research-2026-10-06) and the untouched rest (true OOS
                for rules that were never tuned on DAX). Trades cached to reports/.
Step `prop`   : FTMO 2-Step multi-year emulator, the same state machine as
                backtest/run_s021_ftmo_emulator.py (10%/5% targets, -5% daily, -10% STATIC max
                loss, $70/attempt, payout every 10 trading days at 80%, block bootstrap 5d,
                N=6000, 3 years) for NAS alone, DAX alone and NAS+DAX on one account.

Sizing. NAS: the emulator's floored-lot economics (MPPL=100, MIN_LOT=0.0001). DAX: pure
R-scaling (day % = net_R x risk %); the FTMO GER40.cash min-lot floor is NOT modelled
(at these risk levels and DAX stop sizes it is far from binding). Combo: both legs at
the same risk %, summed per calendar date over the overlap of the two data windows.
Daily-limit checks are on realized end-of-day P/L (as in the S021 emulator), so
intraday floating breaches are undercounted.

Run: python backtest/run_s021_dax.py gate1 ; python backtest/run_s021_dax.py prop
"""
from __future__ import annotations
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from strategies.orb_intraday.config import ORB_BASE, ORB_DAX
from strategies.orb_intraday.engine import (
    load_nsxusd_m1, load_ger40_duka_long_m1, compute_daily_sessions, compute_adr14,
    simulate, trades_to_frame,
)

REPORTS = ROOT / "reports"
DAX_TRADES = REPORTS / "s021_dax_trades.csv"
NAS_TRADES = REPORTS / "s021_nas_trades_for_combo.csv"
SEEN_FROM, SEEN_TO = pd.Timestamp("2023-06-26"), pd.Timestamp("2026-08-11")
MEASURED_DAX_COST_BPS = 2 * 0.2551   # GER40 real spread, bps model (backtest-log 2026-08-12)

# FTMO 2-Step economics -- identical to backtest/run_s021_ftmo_emulator.py
BALANCE = 10000.0
NAS_MPPL, NAS_MIN_LOT = 100.0, 0.0001
PHASE1_TARGET, PHASE2_TARGET = 10.0, 5.0
DAILY_LIMIT_PCT, TOTAL_DD_PCT = -5.0, 10.0
CHALLENGE_FEE, PAYOUT_CYCLE_DAYS, PAYOUT_SPLIT = 70.0, 10, 0.80
YEARS = 3
TRADING_DAYS = 252 * YEARS
BLOCK_LEN, N_SIM, SEED = 5, 6000, 20261002
RM_SET = [0.50, 1.00, 1.50, 2.00, 2.50, 3.00, 4.00, 4.50]


def _trades(m1, cfg):
    t = trades_to_frame(simulate(m1, cfg))
    t["day"] = pd.to_datetime(t["day"]).dt.normalize()
    t["risk_pts"] = (t["entry_price"] - t["stop_price"]).abs()
    t["R"] = t["net_pts"] / t["risk_pts"]
    return t


def _summary(t: pd.DataFrame, label: str) -> None:
    t = t.sort_values("day")
    eq = t["R"].cumsum()
    yrs = t.groupby(t["day"].dt.year)["R"].agg(["size", "sum"])
    print(f"{label}: n={len(t)} win={(t.R > 0).mean():.1%} R/trade={t.R.mean():+.4f} "
          f"total={t.R.sum():+.1f}R maxDD={(eq - eq.cummax()).min():+.1f}R "
          f"years+={(yrs['sum'] > 0).sum()}/{len(yrs)}")
    print("   by year R:", {int(y): round(v, 1) for y, v in yrs["sum"].items()})


def gate1():
    m1 = load_ger40_duka_long_m1(ROOT / "data" / "GER40")
    t = _trades(m1, ORB_DAX)
    t.to_csv(DAX_TRADES, index=False)
    print(f"data {m1.index[0]} .. {m1.index[-1]}  ({len(m1)} bars)")
    _summary(t, "ORB_DAX all, 1.0bp frozen cost")
    seen = (t.day >= SEEN_FROM) & (t.day <= SEEN_TO)
    _summary(t[seen], "  seen 2023-06-26..2026-08-11")
    _summary(t[~seen], "  UNTOUCHED (2015-2023-06 + 2026-08-12..)")
    tm = _trades(m1, ORB_DAX.with_(cost_bps_roundtrip=MEASURED_DAX_COST_BPS))
    _summary(tm, f"ORB_DAX all, measured cost {MEASURED_DAX_COST_BPS:.2f}bp")
    # NAS series for the combo, same engine/frozen config as every S021 report
    tn = _trades(load_nsxusd_m1(ROOT / "data" / "histdata"), ORB_BASE)
    tn.to_csv(NAS_TRADES, index=False)
    _summary(tn, "ORB_BASE NAS (reference)")
    both = pd.merge(t[["day", "R"]], tn[["day", "R"]], on="day", suffixes=("_dax", "_nas"))
    print(f"daily R correlation DAX vs NAS on {len(both)} common trade days: "
          f"{both.R_dax.corr(both.R_nas):+.3f}")


def _bootstrap(rng, day_pct, n_days, n_sim):
    dn = len(day_pct)
    n_blocks = int(np.ceil(n_days / BLOCK_LEN))
    starts = rng.integers(0, max(dn - BLOCK_LEN, 1), size=(n_sim, n_blocks))
    idx = (starts[:, :, None] + np.arange(BLOCK_LEN)[None, None, :]).reshape(n_sim, -1)[:, :n_days]
    return day_pct[np.clip(idx, 0, dn - 1)]


def ftmo_multiyear(day_pct: np.ndarray, rng) -> dict:
    """Same state machine as run_s021_ftmo_emulator.simulate_multiyear (static max loss)."""
    paths = _bootstrap(rng, day_pct, TRADING_DAYS, N_SIM)
    phase = np.ones(N_SIM, dtype=int)
    eq = np.zeros(N_SIM); last_pay = np.zeros(N_SIM)
    since_pay = np.zeros(N_SIM, dtype=int)
    fees = np.full(N_SIM, CHALLENGE_FEE); paid = np.zeros(N_SIM)
    daily_busts = np.zeros(N_SIM, dtype=int); dd_busts = np.zeros(N_SIM, dtype=int)
    ever_funded = np.zeros(N_SIM, dtype=bool); first_funded_day = np.full(N_SIM, -1)
    cum = np.zeros((TRADING_DAYS, N_SIM), dtype=np.float32)
    for day in range(TRADING_DAYS):
        today = paths[:, day]
        eq = eq + today
        d_br = today <= DAILY_LIMIT_PCT
        m_br = (last_pay - eq) >= TOTAL_DD_PCT
        busted = d_br | m_br
        daily_busts += d_br; dd_busts += (m_br & ~d_br)
        target = np.where(phase == 1, PHASE1_TARGET, PHASE2_TARGET)
        passed = (eq >= target) & (phase != 3) & ~busted
        funded = phase == 3
        since_pay = np.where(funded, since_pay + 1, since_pay)
        do_pay = funded & (since_pay >= PAYOUT_CYCLE_DAYS) & ~busted & (eq > last_pay)
        paid = np.where(do_pay, paid + (eq - last_pay) * PAYOUT_SPLIT / 100.0 * BALANCE, paid)
        last_pay = np.where(do_pay, eq, last_pay)
        since_pay = np.where(do_pay, 0, since_pay)
        fees = np.where(busted, fees + CHALLENGE_FEE, fees)
        phase = np.where(busted, 1, phase)
        eq = np.where(busted, 0.0, eq); last_pay = np.where(busted, 0.0, last_pay)
        since_pay = np.where(busted, 0, since_pay)
        to_funded = passed & (phase == 2)
        newly = to_funded & ~ever_funded
        first_funded_day = np.where(newly, day, first_funded_day)
        ever_funded |= to_funded
        phase = np.where(passed & (phase == 1), 2, phase)
        phase = np.where(to_funded, 3, phase)
        eq = np.where(passed, 0.0, eq)
        last_pay = np.where(to_funded, 0.0, last_pay); since_pay = np.where(to_funded, 0, since_pay)
        cum[day] = paid - fees
    out = dict(attempts=float(np.mean(fees / CHALLENGE_FEE)),
               daily_busts=float(daily_busts.mean()), dd_busts=float(dd_busts.mean()),
               p_funded_y1=float(((first_funded_day >= 0) & (first_funded_day < 252)).mean()),
               median_days_to_funded=float(np.median(first_funded_day[first_funded_day >= 0]))
               if ever_funded.any() else float("nan"),
               p_loss_3y=float((cum[-1] < 0).mean()))
    for yr in (1, 2, 3):
        row = cum[yr * 252 - 1]
        out.update({f"y{yr}_median": float(np.median(row)), f"y{yr}_mean": float(row.mean()),
                    f"y{yr}_p5": float(np.percentile(row, 5)), f"y{yr}_p95": float(np.percentile(row, 95))})
    return out


def _daily_pct_dax(t, rm):
    return t.groupby("day")["R"].sum() * rm


def _daily_pct_nas(t, rm):
    risk_amount = BALANCE * rm / 100.0
    lot = np.maximum(risk_amount / (t["risk_pts"].to_numpy() * NAS_MPPL), NAS_MIN_LOT)
    pct = 100.0 * t["net_pts"].to_numpy() * lot * NAS_MPPL / BALANCE
    return pd.Series(pct, index=t["day"]).groupby(level=0).sum()


def _calendar(sessions_idx_list, lo, hi):
    days = sorted(set().union(*[set(i) for i in sessions_idx_list]))
    return pd.DatetimeIndex([d for d in days if lo <= d <= hi])


def prop():
    td = pd.read_csv(DAX_TRADES, parse_dates=["day"])
    tn = pd.read_csv(NAS_TRADES, parse_dates=["day"])
    dax_m1 = load_ger40_duka_long_m1(ROOT / "data" / "GER40")
    dax_days = compute_adr14(compute_daily_sessions(dax_m1, ORB_DAX), ORB_DAX).dropna().index.normalize()
    nas_m1 = load_nsxusd_m1(ROOT / "data" / "histdata")
    nas_days = compute_adr14(compute_daily_sessions(nas_m1, ORB_BASE), ORB_BASE).dropna().index.normalize()
    lo, hi = max(dax_days[0], nas_days[0]), min(dax_days[-1], nas_days[-1])
    cals = {"DAX alone": _calendar([dax_days], dax_days[0], dax_days[-1]),
            "NAS alone": _calendar([nas_days], nas_days[0], nas_days[-1]),
            "NAS+DAX": _calendar([dax_days, nas_days], lo, hi)}
    print(f"FTMO 2-Step $10k, {YEARS}y, N={N_SIM}; combo overlap {lo.date()}..{hi.date()}")
    rows = []
    for name, cal in cals.items():
        rng = np.random.default_rng(SEED)
        for rm in RM_SET:
            parts = []
            if "DAX" in name: parts.append(_daily_pct_dax(td, rm))
            if "NAS" in name: parts.append(_daily_pct_nas(tn, rm))
            dp = sum(p.reindex(cal, fill_value=0.0) for p in parts).to_numpy()
            r = dict(portfolio=name, risk_pct=rm, sessions=len(cal),
                     worst_day_pct=float(dp.min()), **ftmo_multiyear(dp, rng))
            rows.append(r)
            print(f"{name:9s} RM {rm:4.2f}%  worst day {r['worst_day_pct']:+5.2f}%  attempts/3y {r['attempts']:5.2f} "
                  f"(daily {r['daily_busts']:.2f}, maxloss {r['dd_busts']:.2f})  funded<=1y {r['p_funded_y1']:5.1%}  "
                  f"net$ y1 med {r['y1_median']:+6.0f}  y3 med {r['y3_median']:+7.0f} "
                  f"[p5 {r['y3_p5']:+6.0f}, p95 {r['y3_p95']:+7.0f}]  P(loss 3y) {r['p_loss_3y']:5.1%}", flush=True)
    pd.DataFrame(rows).to_csv(REPORTS / "s021_dax_ftmo_emulator.csv", index=False, float_format="%.4f")
    print("wrote reports/s021_dax_ftmo_emulator.csv")


if __name__ == "__main__":
    {"gate1": gate1, "prop": prop}[sys.argv[1]]()
