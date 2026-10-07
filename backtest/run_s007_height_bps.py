"""S007 day filter: Frankfurt-range height cap in POINTS vs in BPS of price.

Question (Anton, 2026-10-05): max_height=100 is an absolute point cap, but
GER40 rose from ~15.8k (2023) to ~24.7k (2026). 100pt was ~63bps of price in
2023 and ~40bps in 2026, so the same rule filtered 0% of 2023 days but 24% of
2026 days. Would a cap in bps of price (scale-invariant) be the better rule?

Method. The height filter is a pure per-day skip with no cross-day state
(daily liquidity levels come from the full df regardless of the filter), so
running the engine ONCE with max_height=None and dropping rows afterwards is
exactly equivalent to running it with the cap. No new engine flag is needed
for the research; height_bps = height / mid * 1e4.

Variants: no cap; point caps 80..160; bps caps 30..70 incl. 48.6bps = 100pt
translated at the whole-sample mean mid (the no-tuning equivalent of the live
cap). Base preset: the live WORKING_S007_NEWSSAFE_MAX8_BE05_OFF2 on the fixed
(ALGODEV-57) engine. Costs: gross, Gate-2 points (0.635pt/side) and Gate-2 bps
(0.2551bps/side, calibrated 2026-08-12). Totals compare fairly across filters
(R/day is over traded days, which a filter changes).

Run: python backtest/run_s007_height_bps.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import numpy as np
import pandas as pd

from strategies.ger40_lonfra import config as C
from strategies.ger40_lonfra import data as D
from strategies.ger40_lonfra.engine import run

LIVE_PRESET = "WORKING_S007_NEWSSAFE_MAX8_BE05_OFF2"
REAL_SPREAD_PER_SIDE = 0.635        # points, Gate 2 (run_s007_algodev57_revalidate.py)
REAL_SPREAD_BPS_PER_SIDE = 0.2551   # bps, Gate 2 bps model (backtest-log 2026-08-12)
LIVE_CAP_POINTS = 100.0
BPS_PER_UNIT = 1e4
POINT_CAPS = (80, 90, 100, 110, 120, 140, 160)
BPS_CAPS = (30, 35, 40, 45, 50, 55, 60, 70)
YEARS = (2023, 2024, 2025, 2026)
OUT_CSV = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "reports", "s007_height_bps.csv")


def stats(r: pd.DataFrame) -> dict:
    r = r.sort_values("date")
    eq = r["day_R"].cumsum()
    years = r.groupby(pd.to_datetime(r["date"]).dt.year)["day_R"].sum()
    out = dict(days=len(r), total_R=r["day_R"].sum(), R_day=r["day_R"].mean(),
               maxDD_R=(eq - eq.cummax()).min(), win_days=(r["day_R"] > 0).mean())
    out.update({f"y{y}": years.get(y, 0.0) for y in YEARS})
    return out


def main():
    df = D.load("duka")
    lv = D.daily_levels(df)
    base = getattr(C, LIVE_PRESET).with_(max_height=None)
    costs = {"gross": {},
             "net_pts": {"spread_per_side": REAL_SPREAD_PER_SIDE},
             "net_bps": {"cost_model": "bps", "spread_bps_per_side": REAL_SPREAD_BPS_PER_SIDE}}
    rows = []
    for cost, extra in costs.items():
        res = run(df, base.with_(**extra), lv)
        h_bps = res["height"] / res["mid"] * BPS_PER_UNIT
        neutral_bps = LIVE_CAP_POINTS / res["mid"].mean() * BPS_PER_UNIT
        variants = [("none", np.ones(len(res), bool))]
        variants += [(f"pts<={p}", (res["height"] <= p).to_numpy()) for p in POINT_CAPS]
        variants += [(f"bps<={b:g}", (h_bps <= b).to_numpy())
                     for b in sorted({*BPS_CAPS, round(neutral_bps, 1)})]
        print(f"\n===== {cost}  (100pt == {neutral_bps:.1f}bps at sample-mean price)")
        for name, keep in variants:
            s = stats(res[keep])
            skipped = (pd.to_datetime(res.loc[~keep, "date"]).dt.year
                       .value_counts().reindex(YEARS, fill_value=0))
            rows.append(dict(cost=cost, variant=name, **s,
                             **{f"skip{y}": int(skipped[y]) for y in YEARS}))
            print(f"{name:10s} days={s['days']:3d} total={s['total_R']:+7.1f} "
                  f"R/day={s['R_day']:+.3f} maxDD={s['maxDD_R']:+7.1f} win={s['win_days']:.1%} "
                  + " ".join(f"{y}:{s[f'y{y}']:+6.1f}" for y in YEARS)
                  + "  skipped/yr " + "/".join(str(int(skipped[y])) for y in YEARS), flush=True)
    out = pd.DataFrame(rows)
    os.makedirs(os.path.dirname(OUT_CSV), exist_ok=True)
    out.to_csv(OUT_CSV, index=False, float_format="%.4f")
    print(f"\nwrote {OUT_CSV}")
    return out


if __name__ == "__main__":
    main()
