"""Gate 0 event study: intraday drift on gap days for liquid US stocks.

Question being tested (cheapest possible version, daily bars only): after a
significant overnight gap on a liquid single stock, does the SAME-DAY
open-to-close move show exploitable drift -- continuation (in the gap's
direction) or fade (against it)?

Why this matters: this open->close drift is the core edge behind intraday
gap/ORB strategies (enter near the open, flat by the close -- the prop-firm
friendly profile with no overnight holds). If there is no measurable drift
on daily data, buying minute data for a full ORB backtest is not justified.

Method (event study, NOT an equity curve):
  - Event: |open/prev_close - 1| >= threshold bucket.
  - Outcome: same-day open->close return, signed for continuation
    (positive = gap direction continued) -- so `fade` PnL is just the
    negative of `continuation` PnL before costs.
  - Liquidity filter uses TRAILING 20-day median dollar volume (known
    before the open -- no look-ahead). Day-t volume is deliberately NOT
    used as a filter because it is unknown at entry time.
  - Costs: fixed round-trip bps subtracted from each event's |PnL| side.

KNOWN LIMITATIONS (inherited from scripts/fetch_stock_universe.py data):
  - Survivorship bias: current large caps only. A stock that gapped down
    and later delisted is missing -- results for downside gaps are likely
    OPTIMISTIC for fade, PESSIMISTIC for continuation. Gate 0 only.
  - yfinance unadjusted opens/closes can be noisy around splits; extreme
    |gap| > 25% events are dropped as split/data artifacts.

Usage:
    python -m backtest.run_gap_study
    python -m backtest.run_gap_study --cost-bps 6 --min-dollar-vol 50e6
"""
from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd

from scripts.fetch_stock_universe import UNIVERSE

ROOT = Path(__file__).resolve().parent.parent
DATA_RAW = ROOT / "data" / "raw"
REPORTS_DIR = ROOT / "reports"

# Gap-size buckets in absolute percent (lower bound inclusive, upper exclusive).
GAP_BUCKETS = [(1.0, 2.0), (2.0, 3.0), (3.0, 5.0), (5.0, 10.0), (10.0, 25.0)]
MAX_ABS_GAP_PCT = 25.0          # above this: treat as split/data artifact, drop
DEFAULT_COST_BPS = 6.0          # round-trip commission+spread+slippage, conservative
DEFAULT_MIN_DOLLAR_VOL = 50e6   # trailing 20d median dollar volume floor
DOLLAR_VOL_WINDOW = 20


def load_ticker(ticker: str) -> pd.DataFrame | None:
    path = DATA_RAW / ticker / f"{ticker}_1d.csv"
    if not path.exists():
        return None
    df = pd.read_csv(path, index_col=0, parse_dates=[0]).sort_index()
    df = df[["open", "high", "low", "close", "volume"]].astype(float)
    return df.dropna(subset=["open", "close"])


def build_events(df: pd.DataFrame, ticker: str, min_dollar_vol: float) -> pd.DataFrame:
    prev_close = df["close"].shift(1)
    gap_pct = (df["open"] / prev_close - 1.0) * 100.0
    oc_ret = df["close"] / df["open"] - 1.0

    # Liquidity known BEFORE the open: shift(1) so day-t volume is excluded.
    dollar_vol = (df["close"] * df["volume"]).rolling(DOLLAR_VOL_WINDOW).median().shift(1)

    events = pd.DataFrame({
        "ticker": ticker,
        "gap_pct": gap_pct,
        "oc_ret": oc_ret,
        "dollar_vol": dollar_vol,
    }).dropna()

    events = events[
        (events["gap_pct"].abs() >= GAP_BUCKETS[0][0])
        & (events["gap_pct"].abs() < MAX_ABS_GAP_PCT)
        & (events["dollar_vol"] >= min_dollar_vol)
    ]
    # Signed for continuation: >0 means the open->close move continued the gap.
    events["cont_ret"] = np.sign(events["gap_pct"]) * events["oc_ret"]
    return events


def bucket_label(abs_gap: float) -> str:
    for lo, hi in GAP_BUCKETS:
        if lo <= abs_gap < hi:
            return f"{lo:g}-{hi:g}%"
    return "other"


def summarize(events: pd.DataFrame, cost_bps: float) -> pd.DataFrame:
    cost = cost_bps / 1e4
    events = events.copy()
    events["abs_gap"] = events["gap_pct"].abs()
    events["bucket"] = events["abs_gap"].map(bucket_label)
    events["direction"] = np.where(events["gap_pct"] > 0, "gap_up", "gap_down")
    events["cont_net"] = events["cont_ret"] - cost
    events["fade_net"] = -events["cont_ret"] - cost

    rows = []
    for (bucket, direction), grp in events.groupby(["bucket", "direction"]):
        n = len(grp)
        for side in ("cont_net", "fade_net"):
            r = grp[side]
            tstat = r.mean() / (r.std(ddof=1) / np.sqrt(n)) if n > 1 and r.std(ddof=1) > 0 else np.nan
            rows.append({
                "bucket": bucket, "direction": direction,
                "side": side.replace("_net", ""), "n": n,
                "mean_bps": r.mean() * 1e4, "median_bps": r.median() * 1e4,
                "win_rate": (r > 0).mean(), "t_stat": tstat,
            })
    out = pd.DataFrame(rows)
    order = [f"{lo:g}-{hi:g}%" for lo, hi in GAP_BUCKETS]
    out["bucket"] = pd.Categorical(out["bucket"], categories=order, ordered=True)
    return out.sort_values(["bucket", "direction", "side"]).reset_index(drop=True)


def per_year(events: pd.DataFrame, cost_bps: float, side: str) -> pd.DataFrame:
    """Yearly stability check for one side (cont or fade), all buckets pooled."""
    cost = cost_bps / 1e4
    signed = events["cont_ret"] if side == "cont" else -events["cont_ret"]
    net = signed - cost
    rows = []
    for year, grp in net.groupby(net.index.year):
        rows.append({"year": year, "n": len(grp), "mean_bps": grp.mean() * 1e4,
                     "win_rate": (grp > 0).mean()})
    return pd.DataFrame(rows).set_index("year")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cost-bps", type=float, default=DEFAULT_COST_BPS)
    ap.add_argument("--min-dollar-vol", type=float, default=DEFAULT_MIN_DOLLAR_VOL)
    args = ap.parse_args()

    all_events, missing = [], []
    for ticker in UNIVERSE:
        df = load_ticker(ticker)
        if df is None:
            missing.append(ticker)
            continue
        all_events.append(build_events(df, ticker, args.min_dollar_vol))
    if missing:
        print(f"WARNING: no data for {len(missing)} tickers: {', '.join(missing)}\n"
              f"Run: python -m scripts.fetch_stock_universe")
    if not all_events:
        raise SystemExit("No data at all -- fetch the universe first.")

    events = pd.concat(all_events).sort_index()
    print(f"Universe: {len(all_events)} tickers, {len(events)} gap events "
          f"({events.index.min().date()}..{events.index.max().date()}), "
          f"cost {args.cost_bps} bps round-trip, "
          f"min trailing $vol {args.min_dollar_vol / 1e6:.0f}M")

    summary = summarize(events, args.cost_bps)
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    summary.to_csv(REPORTS_DIR / "gap_study_summary.csv", index=False)

    print("\n--- Net mean open->close PnL by gap bucket (bps/event, after costs) ---")
    print(f"{'bucket':>8} {'direction':>9} {'side':>5} {'n':>6} "
          f"{'mean':>8} {'median':>8} {'win%':>6} {'t':>6}")
    for _, r in summary.iterrows():
        print(f"{r['bucket']:>8} {r['direction']:>9} {r['side']:>5} {r['n']:>6.0f} "
              f"{r['mean_bps']:>+8.1f} {r['median_bps']:>+8.1f} "
              f"{r['win_rate']:>6.1%} {r['t_stat']:>6.2f}")

    for side in ("cont", "fade"):
        yearly = per_year(events, args.cost_bps, side)
        yearly.to_csv(REPORTS_DIR / f"gap_study_yearly_{side}.csv")
        pos_years = int((yearly["mean_bps"] > 0).sum())
        print(f"\n--- Per-year net mean, side={side} (all buckets pooled): "
              f"{pos_years}/{len(yearly)} positive years ---")
        for year, r in yearly.iterrows():
            flag = "" if r["mean_bps"] > 0 else "  <-- negative"
            print(f"  {year}: n={r['n']:5.0f}  mean {r['mean_bps']:+7.1f} bps  "
                  f"win {r['win_rate']:.1%}{flag}")

    print("\nReports: reports/gap_study_summary.csv, reports/gap_study_yearly_*.csv")
    print("REMINDER: survivorship-biased universe -- Gate 0 direction check only.")


if __name__ == "__main__":
    main()

