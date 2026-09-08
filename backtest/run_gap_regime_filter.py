"""Gate 0 follow-up #2: market-regime filters for the no-news gap-fade slice.

Candidate from run_gap_earnings_split.py: fade (buy) no-news 2-5% down gaps
on liquid US large caps, open->close, no overnight. It averaged +15.5 bps net
but had negative years (2011 notably). Question: does a simple "is the whole
market stormy?" filter, computable BEFORE entry, remove the bad years without
destroying the sample size?

Filters tested (all use only information available at the stock's open):
  F1 trend  -- SPY previous close >= its 200-day average (classic risk-on).
  F2 calm   -- SPY 20-day realized volatility (annualized, through yesterday)
               below a threshold.
  F3 spygap -- SPY itself is NOT gapping down hard today (its open is known
               at entry time). A stock down 2-5% while the whole market is
               down is a market move, not an idiosyncratic dislocation.

Data prerequisites: stock universe + earnings dates (see run_gap_study.py,
run_gap_earnings_split.py) and data/raw/SPY/SPYd1.csv.

Usage:
    python -m backtest.run_gap_regime_filter
    python -m backtest.run_gap_regime_filter --calm-vol-max 30 --spy-gap-min -0.3
"""
from __future__ import annotations

import argparse

import numpy as np
import pandas as pd

from backtest.run_gap_earnings_split import collect_events, stats
from backtest.run_gap_study import DEFAULT_COST_BPS, DEFAULT_MIN_DOLLAR_VOL, REPORTS_DIR, ROOT

SPY_FILE = ROOT / "data" / "raw" / "SPY" / "SPYd1.csv"


def load_spy_regime() -> pd.DataFrame:
    spy = pd.read_csv(SPY_FILE, parse_dates=["date"]).set_index("date").sort_index()
    close, open_ = spy["close"], spy["open"]
    daily_ret = close.pct_change()
    # All shifted so that value at date t uses data known BEFORE t's open.
    regime = pd.DataFrame({
        "trend_ok": (close >= close.rolling(200).mean()).shift(1),
        "vol20_ann_pct": (daily_ret.rolling(20).std() * np.sqrt(252) * 100).shift(1),
        # SPY's own gap at date t IS known at the open of date t -- no shift.
        "spy_gap_pct": (open_ / close.shift(1) - 1.0) * 100.0,
    })
    return regime


def report(name: str, net: pd.Series, baseline_n: int) -> None:
    s = stats(net)
    yearly = net.groupby(net.index.year).agg(["size", "mean"])
    neg_years = sorted(int(y) for y, m in yearly["mean"].items() if m <= 0)
    print(f"\n{name}")
    print(f"  kept {s['n']}/{baseline_n} events ({s['n'] / baseline_n:.0%}), "
          f"mean {s['mean_bps']:+.1f} bps, win {s['win_rate']:.1%}, t={s['t_stat']:.2f}")
    print(f"  negative years ({len(neg_years)}/{len(yearly)}): "
          f"{', '.join(map(str, neg_years)) if neg_years else 'none'}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cost-bps", type=float, default=DEFAULT_COST_BPS)
    ap.add_argument("--min-dollar-vol", type=float, default=DEFAULT_MIN_DOLLAR_VOL)
    ap.add_argument("--calm-vol-max", type=float, default=25.0,
                    help="F2: max SPY 20d annualized vol, percent")
    ap.add_argument("--spy-gap-min", type=float, default=-0.5,
                    help="F3: min SPY same-day gap, percent")
    args = ap.parse_args()
    cost = args.cost_bps / 1e4

    events = collect_events(args.min_dollar_vol)
    cand = events[(events["gap_pct"] <= -2.0) & (events["gap_pct"] > -5.0)
                  & (~events["is_earnings"])].copy()
    cand["fade_net"] = -cand["cont_ret"] - cost

    regime = load_spy_regime()
    cand = cand.join(regime, how="inner")  # drops events beyond SPY coverage
    cand = cand.dropna(subset=["trend_ok", "vol20_ann_pct", "spy_gap_pct"])
    baseline_n = len(cand)
    print(f"\nCandidate events with regime data: {baseline_n} "
          f"({cand.index.min().date()}..{cand.index.max().date()})")

    f1 = cand["trend_ok"].astype(bool)
    f2 = cand["vol20_ann_pct"] < args.calm_vol_max
    f3 = cand["spy_gap_pct"] > args.spy_gap_min

    scenarios = [
        ("BASELINE (no filter)", pd.Series(True, index=cand.index)),
        ("F1 trend: SPY above 200d avg", f1),
        (f"F2 calm: SPY 20d vol < {args.calm_vol_max:g}%", f2),
        (f"F3 spygap: SPY gap > {args.spy_gap_min:g}%", f3),
        ("F1+F2", f1 & f2),
        ("F1+F3", f1 & f3),
        ("F2+F3", f2 & f3),
        ("F1+F2+F3", f1 & f2 & f3),
    ]
    rows = []
    for name, mask in scenarios:
        net = cand.loc[mask, "fade_net"]
        report(name, net, baseline_n)
        rows.append({"scenario": name, **stats(net)})

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_csv(REPORTS_DIR / "gap_regime_filter_summary.csv", index=False)
    print("\nReport: reports/gap_regime_filter_summary.csv")
    print("REMINDER: survivorship-biased universe -- Gate 0 direction check only.")


if __name__ == "__main__":
    main()

