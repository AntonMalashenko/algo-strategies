"""Tail-risk modifiers for S007 (ALGODEV-14 follow-up): validation gates.

Two modifiers aimed at the worst-day tail found in the ALGODEV-14 worst-day
study (all 30 worst days = B double failures: primary leg fully stopped, then
the recovery leg fully stopped too):

1. RECOVERY CAP -- cfg.max_recovery_positions=2: the recovery leg may hold at
   most 2 positions (first entry + 1 add). Engine flag, default None (off).
2. HTF HALF-RISK -- trade counter-H1-trend days at half size. The flag is
   computed causally (H1 bars completed strictly before 10:00 Kyiv, close vs
   SMA(50)); since R accounting is linear in position size, half size on a
   flagged day = day_R * 0.5 exactly, so this needs no engine change here.
   Rationale: counter-H1 days carry 2x the worst-day tail rate (10.1% vs 4.9%)
   at the SAME expectancy, i.e. the flag predicts day variance, not sign.

PRE-DECLARED thresholds (fixed before this script was first run): CAP=2,
H1 SMA window=50, half-risk weight=0.5. HONEST CAVEAT: CAP=2 was suggested by
a counterfactual on the full sample (see config.py comment), so the sweep
below reports per-year stability rather than claiming untouched-OOS purity.

VERDICT (2026-08-31, first run of this script):
- HTF HALF-RISK: REJECTED. It cuts profit MORE than it cuts drawdown
  (LIQFLOOR: sum -18.5% for maxDD -15.3%; NEWSSAFE: -19.7% for -11.8%), i.e.
  it is dominated by simply trading smaller across the board. The counter-H1
  flag predicts day variance but monetizing that via day-level sizing loses.
- RECOVERY CAP=2: ACCEPTED for the prop worst-day axis only (see
  config.py::max_recovery_positions comment and WORKING_S007_PROP preset):
  -6.1..-6.7% profit for -25% worst day (-9.03 -> -6.75R), beating uniform
  scaling on that axis; maxDD ~unchanged. Combined cap+half-risk is dominated.

Includes a no-look-ahead check for the H1 flag (truncate M1, flags must match).

Usage: .venv/bin/python backtest/run_s007_tailrisk.py
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from strategies.ger40_lonfra import config as C
from strategies.ger40_lonfra import data as D
from strategies.ger40_lonfra.engine import run

REAL_SPREAD_PER_SIDE = 0.635  # Gate 2 standard (1.27pt round-trip / 2)
RECOVERY_CAP = 2              # pre-declared
H1_SMA_WINDOW = 50            # pre-declared
HALF_RISK_WEIGHT = 0.5        # pre-declared
ENTRY_CUTOFF = "10:00"        # flag uses H1 bars completed strictly before this


def max_dd(day_R: pd.Series) -> float:
    if len(day_R) == 0:
        return 0.0
    cum = day_R.cumsum().to_numpy()
    return float((cum - np.maximum.accumulate(cum)).min())


def h1_counter_trend_flags(m1: pd.DataFrame) -> pd.Series:
    """Per (date, direction) -> is the trade counter the H1 SMA(50) trend, using
    only H1 bars fully completed before ENTRY_CUTOFF Kyiv on that date. Returns
    a Series indexed by date with values +1 (close>sma) / -1 (close<=sma): the
    H1 trend sign as of 10:00; counter-trend = trade direction disagrees."""
    x = m1.set_index("dt")
    h1 = pd.DataFrame({
        "close": x["close"].resample("1h").last(),
    }).dropna()
    h1["sma"] = h1["close"].rolling(H1_SMA_WINDOW).mean()
    h1["end"] = h1.index + pd.Timedelta(hours=1)
    trend = {}
    for d in sorted(m1["date_only"].unique()):
        t10 = pd.Timestamp(f"{d} {ENTRY_CUTOFF}")
        done = h1[h1["end"] <= t10]
        if len(done) == 0 or np.isnan(done["sma"].iloc[-1]):
            trend[d] = 0
        else:
            trend[d] = 1 if done["close"].iloc[-1] > done["sma"].iloc[-1] else -1
    return pd.Series(trend)


def apply_half_risk(res: pd.DataFrame, trend: pd.Series) -> pd.Series:
    tr = res["date"].map(trend).fillna(0)
    dir_sign = np.where(res["direction"] == "up", 1, -1)
    counter = (tr != 0) & (tr != dir_sign)
    return res["day_R"] * np.where(counter, HALF_RISK_WEIGHT, 1.0), counter


def report(name: str, day_R: pd.Series, dates: pd.Series) -> None:
    yrs = pd.to_datetime(dates.astype(str)).dt.year
    yr = {int(y): round(float(s.sum()), 1) for y, s in day_R.groupby(yrs)}
    print(f"{name:42s} net {day_R.mean():+.4f} sum {day_R.sum():+7.1f}R "
          f"maxDD {max_dd(day_R):6.1f}R worst {day_R.min():+.2f}R years {yr}")


def gate0_flag(m1: pd.DataFrame, trend_full: pd.Series) -> None:
    cutoff = "2025-06-01"
    m1_cut = m1[m1["dt"] <= pd.Timestamp(cutoff)]
    trend_cut = h1_counter_trend_flags(m1_cut)
    common = [d for d in trend_cut.index if str(d) < "2025-05-25"]  # margin: 1 week
    a = trend_full.loc[common]
    b = trend_cut.loc[common]
    assert (a == b).all(), "H1 trend flag differs after truncation -> look-ahead!"
    print(f"Gate 0 (H1 flag): OK -- {len(common)} dates identical after truncating at {cutoff}")


if __name__ == "__main__":
    m1 = D.load("duka")
    lv = D.daily_levels(m1)
    trend = h1_counter_trend_flags(m1)
    gate0_flag(m1, trend)

    for label, base in [("LIQFLOOR", C.WORKING_S007_LIQFLOOR),
                        ("NEWSSAFE", C.WORKING_S007_NEWSSAFE)]:
        print(f"\n=== {label} (net, spread {REAL_SPREAD_PER_SIDE}/side) ===")
        base_net = base.with_(spread_per_side=REAL_SPREAD_PER_SIDE)
        res_base = run(m1, base_net, lv)
        report("baseline", res_base["day_R"], res_base["date"])

        # recovery-cap sweep (2 = pre-declared candidate; 1,3 for context)
        for cap in [3, 2, 1]:
            res_cap = run(m1, base_net.with_(max_recovery_positions=cap), lv)
            report(f"rec cap={cap}", res_cap["day_R"], res_cap["date"])

        # HTF half-risk on the baseline
        hr, counter = apply_half_risk(res_base, trend)
        print(f"  (counter-H1 days: {counter.sum()}/{len(res_base)})")
        report("half-risk counter-H1", hr, res_base["date"])

        # combined: cap=2 + half-risk
        res_c2 = run(m1, base_net.with_(max_recovery_positions=RECOVERY_CAP), lv)
        hr2, _ = apply_half_risk(res_c2, trend)
        report(f"rec cap={RECOVERY_CAP} + half-risk", hr2, res_c2["date"])
