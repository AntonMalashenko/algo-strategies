"""Gate 0 follow-up: split gap-day drift into earnings vs no-news days.

Refines backtest/run_gap_study.py's finding (fading 2-5% down gaps on liquid
US large caps showed +11 bps/event net). Question: does that edge come from
earnings-reaction days or from "no-news" gaps? The answer decides what the
strategy actually is and which risk filter it needs.

Earnings labeling: an event is tagged ``earnings`` when the gap day is the
announcement day itself OR the next trading day (announcement timestamps
from Yahoo do not reliably distinguish before-open vs after-close, so both
candidate reaction days are tagged). Over-tagging is deliberate: it keeps
the ``no_news`` bucket clean, and that purity matters more here than the
exact size of the ``earnings`` bucket. Tickers without a cached earnings
file are EXCLUDED entirely (unknown label would contaminate both buckets).

Data prerequisites:
    python -m scripts.fetch_stock_universe
    python -m scripts.fetch_earnings_dates

Usage:
    python -m backtest.run_gap_earnings_split
    python -m backtest.run_gap_earnings_split --cost-bps 6
"""
from __future__ import annotations

import argparse

import numpy as np
import pandas as pd

from backtest.run_gap_study import (
    DEFAULT_COST_BPS,
    DEFAULT_MIN_DOLLAR_VOL,
    GAP_BUCKETS,
    REPORTS_DIR,
    build_events,
    bucket_label,
    load_ticker,
)
from scripts.fetch_stock_universe import UNIVERSE
from utils.data import DATA_RAW


def load_earnings_days(ticker: str) -> pd.DatetimeIndex | None:
    """Trading-day candidates affected by an announcement (day and day after)."""
    path = DATA_RAW / ticker / f"{ticker}_earnings.csv"
    if not path.exists():
        return None
    dates = pd.to_datetime(pd.read_csv(path)["date"]).dt.normalize()
    affected = pd.DatetimeIndex(dates).union(pd.DatetimeIndex(dates) + pd.offsets.BDay(1))
    return affected.unique()


def collect_events(min_dollar_vol: float) -> pd.DataFrame:
    frames, skipped = [], []
    for ticker in UNIVERSE:
        df = load_ticker(ticker)
        earnings = load_earnings_days(ticker)
        if df is None or earnings is None:
            skipped.append(ticker)
            continue
        ev = build_events(df, ticker, min_dollar_vol)
        ev["is_earnings"] = ev.index.normalize().isin(earnings)
        frames.append(ev)
    if skipped:
        print(f"Excluded (no price or earnings data): {', '.join(skipped)}")
    return pd.concat(frames).sort_index()


def stats(net: pd.Series) -> dict:
    n = len(net)
    t = net.mean() / (net.std(ddof=1) / np.sqrt(n)) if n > 1 and net.std(ddof=1) > 0 else np.nan
    return {"n": n, "mean_bps": net.mean() * 1e4, "median_bps": net.median() * 1e4,
            "win_rate": (net > 0).mean(), "t_stat": t}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cost-bps", type=float, default=DEFAULT_COST_BPS)
    ap.add_argument("--min-dollar-vol", type=float, default=DEFAULT_MIN_DOLLAR_VOL)
    args = ap.parse_args()
    cost = args.cost_bps / 1e4

    events = collect_events(args.min_dollar_vol)
    n_earn = int(events["is_earnings"].sum())
    print(f"{len(events)} gap events, {n_earn} tagged earnings "
          f"({n_earn / len(events):.1%}), cost {args.cost_bps} bps round-trip")

    # Main split: fade PnL by bucket x direction x earnings label.
    events = events.copy()
    events["bucket"] = events["gap_pct"].abs().map(bucket_label)
    events["direction"] = np.where(events["gap_pct"] > 0, "gap_up", "gap_down")
    events["label"] = np.where(events["is_earnings"], "earnings", "no_news")
    events["fade_net"] = -events["cont_ret"] - cost

    rows = []
    for (bucket, direction, label), grp in events.groupby(["bucket", "direction", "label"]):
        rows.append({"bucket": bucket, "direction": direction, "label": label,
                     **stats(grp["fade_net"])})
    summary = pd.DataFrame(rows)
    order = [f"{lo:g}-{hi:g}%" for lo, hi in GAP_BUCKETS]
    summary["bucket"] = pd.Categorical(summary["bucket"], categories=order, ordered=True)
    summary = summary.sort_values(["bucket", "direction", "label"]).reset_index(drop=True)

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    summary.to_csv(REPORTS_DIR / "gap_earnings_split_summary.csv", index=False)

    print("\n--- FADE net PnL (bps/event after costs), earnings vs no-news ---")
    print(f"{'bucket':>8} {'direction':>9} {'label':>9} {'n':>6} "
          f"{'mean':>8} {'median':>8} {'win%':>6} {'t':>6}")
    for _, r in summary.iterrows():
        print(f"{r['bucket']:>8} {r['direction']:>9} {r['label']:>9} {r['n']:>6.0f} "
              f"{r['mean_bps']:>+8.1f} {r['median_bps']:>+8.1f} "
              f"{r['win_rate']:>6.1%} {r['t_stat']:>6.2f}")

    # Per-year stability for the candidate slice: fade no-news 2-5% down gaps.
    cand = events[(events["gap_pct"] <= -2.0) & (events["gap_pct"] > -5.0)
                  & (events["label"] == "no_news")]["fade_net"]
    yearly = cand.groupby(cand.index.year).agg(["size", "mean"])
    yearly.columns = ["n", "mean"]
    yearly.to_csv(REPORTS_DIR / "gap_earnings_split_yearly_candidate.csv")
    pos = int((yearly["mean"] > 0).sum())
    s = stats(cand)
    print(f"\n--- Candidate slice: FADE no-news gap_down 2-5% ---")
    print(f"n={s['n']}, mean {s['mean_bps']:+.1f} bps, win {s['win_rate']:.1%}, "
          f"t={s['t_stat']:.2f}, positive years {pos}/{len(yearly)}")
    for year, r in yearly.iterrows():
        flag = "" if r["mean"] > 0 else "  <-- negative"
        print(f"  {year}: n={r['n']:4.0f}  {r['mean'] * 1e4:+7.1f} bps{flag}")

    print("\nReports: reports/gap_earnings_split_summary.csv, "
          "reports/gap_earnings_split_yearly_candidate.csv")
    print("REMINDER: survivorship-biased universe -- Gate 0 direction check only.")


if __name__ == "__main__":
    main()

