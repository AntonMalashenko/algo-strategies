"""S021 breakeven-stop modifier (breakeven_at_r): Gate 0 regression, Gate 1 backtest
sweep, solo Gate 3, and joint (S007+S021) Gate 3 -- all for BASE (breakeven off) vs
BE@0.5R / BE@1.0R / BE@1.5R.

Requested by Anton, 2026-09-28, in a short follow-up chain after the S007+S021 combo
Gate 3 work (run_s007_s021_combo_propscheme.py): "а мы тестировали бу в 021?" (had
breakeven ever been tested on S021) -> "давай" (go ahead, add and test it) ->
"проверь при достижении 0.5, 1, и 1.5 рр" (check specifically at 0.5R/1.0R/1.5R
trigger levels) -- this script is that check. Unlike S007 (strategy-passport-S007.md
sec 4d), S021 had NO breakeven mechanism at all before this: engine.simulate()'s
exit loop only ever checked the original fixed stop. See config.py's breakeven_at_r
field comment for why this differs from S007 and the mechanical hypothesis tested
here (S021 has no TP and rides to the 15:59 time-exit, so moving to breakeven early
mostly just caps the winners that later run, without protecting much on the loss
side, since bad days are usually the original stop being hit outright).

VERDICT (2026-09-28): REJECTED at all three levels. See config.py's breakeven_at_r
field comment and claude/backtest-log.md 2026-09-28 for the exact numbers this
script produces and the reasoning. Kept as documented off-by-default presets
(ORB_BE05/ORB_BE10/ORB_BE15), not deleted, per this project's archive-don't-delete
convention for rejected modifiers.

STRUCTURE (mirrors run_s007_breakeven.py's shape, adapted to S021's simpler
1-trade/day engine):
  Gate 0 -- regression: ORB_BASE (breakeven_at_r=None) must still reproduce the
    documented reference trade count/sum exactly; the breakeven code path in
    engine.simulate() is provably a no-op when the flag is unset.
  Gate 1 -- backtest sweep: mean R/trade, win rate, per-year sign, max drawdown in
    R, and how often breakeven actually armed/exited, on real 1bp cost over the
    full 2019-2026 history (same window as run_s021_propscheme.py).
  Gate 3 solo -- reuses build_daily_R()/prop_sim() from run_s021_propscheme.py
    unchanged, at the same 0.50%/0.75% risk levels and same SEED, so absolute
    numbers stay directly comparable to that script's own documented output.
  Gate 3 combo -- reuses build_s007_frame()/align()/joint_prop_sim() from
    run_s007_s021_combo_propscheme.py unchanged, at the live risk settings
    (S007=0.25%, S021=0.50%) and same SEED, so absolute numbers stay directly
    comparable to that script's own documented output.

Usage: python3 backtest/run_s021_breakeven.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from strategies.orb_intraday.config import ORB_BASE, ORB_BE05, ORB_BE10, ORB_BE15, OrbConfig
from strategies.orb_intraday.engine import (
    load_nsxusd_m1, compute_daily_sessions, compute_adr14, simulate, trades_to_frame,
)

from backtest.run_s021_propscheme import build_daily_R, prop_sim, DATA_DIR
from backtest.run_s021_propscheme import SEED as SOLO_SEED
from backtest.run_s007_s021_combo_propscheme import (
    build_s007_frame, align, joint_prop_sim,
    S007_LIVE_RISK_PCT, S021_LIVE_RISK_PCT, SEED as COMBO_SEED,
)

BE_LEVELS = [0.5, 1.0, 1.5]  # exactly the three levels Anton asked for -- see module docstring
BE_PRESETS = {0.5: ORB_BE05, 1.0: ORB_BE10, 1.5: ORB_BE15}

SOLO_RISK_LEVELS = (0.50, 0.75)  # same range run_s021_propscheme.py's own passport-recommended band uses

# Regression anchor: ORB_BASE's exact reference numbers, first proven when the
# breakeven code path was added to engine.simulate() (2026-09-28) -- see
# strategies/orb_intraday/config.py's breakeven_at_r field comment.
REGRESSION_N = 1518
REGRESSION_SUM_NET_PTS = 16220.614


def regression_check(m1: pd.DataFrame) -> None:
    trades = trades_to_frame(simulate(m1, ORB_BASE))
    n = len(trades)
    s = round(float(trades["net_pts"].sum()), 3)
    be_any = bool(trades["be_moved"].any()) if "be_moved" in trades.columns else False
    ok = (n == REGRESSION_N) and (abs(s - REGRESSION_SUM_NET_PTS) < 1e-3) and not be_any
    print(f"Regression check: n={n} sum_net_pts={s:.3f} be_moved.any()={be_any}  "
          f"expected n={REGRESSION_N} sum={REGRESSION_SUM_NET_PTS} be_moved=False  "
          f"-> {'OK' if ok else 'FAIL'}")
    if not ok:
        raise SystemExit("Regression FAILED -- the breakeven modifier changed the "
                          "ORB_BASE (breakeven-off) path. Stop and investigate before "
                          "trusting any BE@*R numbers below.")


def max_dd_R(r_trades: np.ndarray) -> float:
    if len(r_trades) == 0:
        return 0.0
    cum = np.cumsum(r_trades)
    return float((cum - np.maximum.accumulate(cum)).min())


def per_trade_R(trades: pd.DataFrame, cfg: OrbConfig) -> np.ndarray:
    stop_dist = trades["adr14"] * cfg.stop_adr_mult
    return (trades["net_pts"] / stop_dist).to_numpy()


def yearly_R(trades: pd.DataFrame, r: np.ndarray) -> dict:
    if len(trades) == 0:
        return {}
    yrs = pd.to_datetime(trades["day"]).dt.year.to_numpy()
    out: dict[int, float] = {}
    for y in sorted(set(yrs)):
        out[int(y)] = round(float(r[yrs == y].sum()), 2)
    return out


def report_gate1(name: str, cfg: OrbConfig, m1: pd.DataFrame) -> dict | None:
    trades = trades_to_frame(simulate(m1, cfg))
    if len(trades) == 0:
        print(f"{name:20s}  (no trades)")
        return None
    r = per_trade_R(trades, cfg)
    n = len(trades)
    win_rate = 100 * float((r > 0).mean())
    mean_r = float(r.mean())
    dd = max_dd_R(r)
    yrs = yearly_R(trades, r)
    n_pos_years = sum(1 for v in yrs.values() if v > 0)
    be_armed_pct = 100 * float(trades["be_moved"].mean())
    be_exit_pct = 100 * float((trades["exit_reason"] == "breakeven").mean())
    print(f"{name:20s}  n={n:4d}  mean_R={mean_r:+.4f}  WR={win_rate:4.1f}%  "
          f"maxDD={dd:+7.2f}R  years+={n_pos_years}/{len(yrs)}  "
          f"BE-armed={be_armed_pct:4.1f}%  BE-stop-exit={be_exit_pct:4.1f}%  years={yrs}")
    return dict(name=name, n=n, mean_R=round(mean_r, 4), win_rate_pct=round(win_rate, 1),
                maxDD_R=round(dd, 2), n_pos_years=n_pos_years, n_years=len(yrs),
                be_armed_pct=round(be_armed_pct, 1), be_stop_exit_pct=round(be_exit_pct, 1))


def solo_gate3_sweep(m1: pd.DataFrame) -> pd.DataFrame:
    rows = []
    rng = np.random.default_rng(SOLO_SEED)
    for name, cfg in [("BASE", ORB_BASE)] + [(f"BE@{lv}R", BE_PRESETS[lv]) for lv in BE_LEVELS]:
        r_series = build_daily_R(m1, cfg)
        for risk_pct in SOLO_RISK_LEVELS:
            stats = prop_sim(r_series, risk_pct, rng)
            stats.update(name=name, risk_pct=risk_pct)
            rows.append(stats)
            print(f"  solo Gate3  {name:8s} risk={risk_pct:.2f}%  "
                  f"cashout={stats['cashout_pct']:5.1f}%  daily-bust={stats['bust_daily_pct']:4.1f}%  "
                  f"dd-bust={stats['bust_dd_pct']:4.1f}%  timeout={stats['timeout_pct']:4.1f}%  "
                  f"med.days(cash)={stats['median_days_cashout']:4.0f}")
    return pd.DataFrame(rows)


def combo_gate3_sweep(m1: pd.DataFrame) -> pd.DataFrame:
    s007_dates, s007_pos_R_raw, s007_pos_mask_raw = build_s007_frame()
    rows = []
    rng = np.random.default_rng(COMBO_SEED)
    for name, cfg in [("BASE", ORB_BASE)] + [(f"BE@{lv}R", BE_PRESETS[lv]) for lv in BE_LEVELS]:
        r_series = build_daily_R(m1, cfg)
        daily = compute_daily_sessions(m1, cfg)
        adr14 = compute_adr14(daily, cfg)
        valid_days = adr14.dropna().index
        s021_series = pd.Series(r_series, index=pd.to_datetime(valid_days))
        calendar, s007_pos_aligned, s007_mask_aligned, s021_aligned = align(
            s007_dates, s007_pos_R_raw, s007_pos_mask_raw, s021_series)
        stats = joint_prop_sim(s007_pos_aligned, s007_mask_aligned, s021_aligned,
                                S007_LIVE_RISK_PCT, S021_LIVE_RISK_PCT, rng)
        stats.update(name=name)
        rows.append(stats)
        print(f"  combo Gate3 {name:8s} (S007={S007_LIVE_RISK_PCT}% S021={S021_LIVE_RISK_PCT}%)  "
              f"cashout={stats['cashout_pct']:5.2f}%  daily-bust={stats['bust_daily_pct']:5.2f}%  "
              f"dd-bust={stats['bust_dd_pct']:5.2f}%  med.days(cash)={stats['median_days_cashout']:4.0f}")
    return pd.DataFrame(rows)


if __name__ == "__main__":
    m1 = load_nsxusd_m1(DATA_DIR)
    print(f"NSXUSD M1 (fixed-EST anchor, canonical): {m1.index.min()} .. {m1.index.max()} ({len(m1)} bars)\n")

    print("=== Gate 0: regression check (ORB_BASE must be unaffected by the new breakeven code path) ===")
    regression_check(m1)

    print("\n=== Gate 1: backtest sweep, BASE vs BE@0.5R/1.0R/1.5R (real 1bp cost, full history) ===")
    for name, cfg in [("BASE", ORB_BASE)] + [(f"BE@{lv}R", BE_PRESETS[lv]) for lv in BE_LEVELS]:
        report_gate1(name, cfg, m1)

    print("\n=== Gate 3 (solo): prop-survivability sweep at 0.50%/0.75% risk ===")
    df_solo = solo_gate3_sweep(m1)

    print("\n=== Gate 3 (combo, S007+S021 joint, live risk settings): prop-survivability ===")
    df_combo = combo_gate3_sweep(m1)

    out_dir = Path(__file__).resolve().parent.parent / "reports"
    out_dir.mkdir(exist_ok=True)
    df_solo.to_csv(out_dir / "s021_breakeven_gate3_solo.csv", index=False)
    df_combo.to_csv(out_dir / "s021_breakeven_gate3_combo.csv", index=False)
    print(f"\nSummaries written to {out_dir}/s021_breakeven_gate3_solo.csv "
          f"and {out_dir}/s021_breakeven_gate3_combo.csv")

    print("\nVERDICT (2026-09-28): breakeven REJECTED at all three tested levels -- see "
          "strategies/orb_intraday/config.py's breakeven_at_r field comment and "
          "claude/backtest-log.md 2026-09-28 for the full numbers and reasoning.")
