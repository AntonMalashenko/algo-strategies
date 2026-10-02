"""ALGODEV-57 re-validation: every S007 preset in config.py on the FIXED engine
(skip_wrong_side_stop=True, the new default) vs the pre-fix engine (False),
gross and Gate-2 net (real spread 0.635 pt/side). Prints total R, R/day,
maxDD, share of winning days and per-year R, and writes one CSV row per
(preset, cost, engine) to reports/s007_algodev57_revalidate.csv.

The pre-fix engine booked positions born with their (shared, mid_range) stop
already beyond their own entry as an instant +1R -- see
StrategyConfig.skip_wrong_side_stop and backtest/run_s007_wrongside_adds.py,
whose first-order estimate this full re-simulation replaces (the fix also
frees max_positions slots / daily-budget room the phantom adds used to take).

R/day is the mean over traded days (the day count is printed; the fix can
drop a day entirely when its first entry would already be wrong-side).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pandas as pd

from strategies.ger40_lonfra import config as C
from strategies.ger40_lonfra import data as D
from strategies.ger40_lonfra.engine import run

REAL_SPREAD_PER_SIDE = 0.635   # Gate 2, same as backtest/run_s007_breakeven.py
LIVE_PRESET = "WORKING_S007_NEWSSAFE_MAX8_BE05_OFF2"
OUT_CSV = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "reports", "s007_algodev57_revalidate.csv")


def presets():
    """All research presets; REF_* are regression-only (pinned to the old engine)."""
    names = sorted(n for n in dir(C)
                   if n.isupper() and isinstance(getattr(C, n), C.StrategyConfig)
                   and not n.startswith("REF_"))
    names.remove(LIVE_PRESET)
    return [LIVE_PRESET, *names]


def stats(res):
    if len(res) == 0:
        return dict(days=0, total_R=0.0, R_day=0.0, maxDD_R=0.0, win_days=0.0)
    r = res.sort_values("date")
    eq = r["day_R"].cumsum()
    years = r.groupby(pd.to_datetime(r["date"]).dt.year)["day_R"].sum()
    out = dict(days=len(r), total_R=r["day_R"].sum(), R_day=r["day_R"].mean(),
               maxDD_R=(eq - eq.cummax()).min(), win_days=(r["day_R"] > 0).mean())
    out.update({f"y{y}": v for y, v in years.items()})
    return out


def main(only=None):
    df = D.load("duka")
    lv = D.daily_levels(df)
    rows = []
    for name in (only or presets()):
        base = getattr(C, name)
        for cost, extra in (("gross", {}), ("net", {"spread_per_side": REAL_SPREAD_PER_SIDE})):
            for engine, flag in (("old", False), ("fixed", True)):
                s = stats(run(df, base.with_(skip_wrong_side_stop=flag, **extra), lv))
                rows.append(dict(preset=name, cost=cost, engine=engine, **s))
                yrs = " ".join(f"{k[1:]}:{v:+7.1f}" for k, v in s.items() if k.startswith("y"))
                print(f"{name:38s} {cost:5s} {engine:5s} days={s['days']:3d} "
                      f"total={s['total_R']:+8.1f} R/day={s['R_day']:+.3f} "
                      f"maxDD={s['maxDD_R']:+7.1f} win={s['win_days']:.1%}  {yrs}", flush=True)
    out = pd.DataFrame(rows)
    os.makedirs(os.path.dirname(OUT_CSV), exist_ok=True)
    out.to_csv(OUT_CSV, index=False, float_format="%.4f")
    print(f"\nwrote {OUT_CSV}")
    return out


if __name__ == "__main__":
    main(sys.argv[1:] or None)
