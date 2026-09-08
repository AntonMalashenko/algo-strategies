"""Gate 0 follow-up #3: intraday adverse excursion and stop-loss grid for the
no-news gap-fade candidate, using the daily LOW as worst-case intraday path.

For a buy-at-open / sell-at-close trade the day's low bounds the intraday
drawdown exactly: MAE = low/open - 1 (no minute data needed for this bound).
Two things are measured on the candidate slice (calm market, no-news 2-5%
down gap, see docs/HANDOFF_GAP_FADE_RESEARCH.md):

1. MAE distribution -- how deep trades sink before the close, and how often
   a single trade would threaten a prop-firm daily loss limit.
2. Stop-loss grid -- if a stop at -X% from entry is assumed FILLED AT the
   stop price whenever low <= stop level, how do mean PnL / win rate / worst
   day change? This fill assumption ignores intraday sequencing (a trade
   could dip to the stop AFTER banking profit -- unknowable from daily bars)
   and gap-through slippage, so treat results as a first-order estimate;
   minute data must confirm before any go decision.

Also reports the worst PORTFOLIO day per stop level (all same-day events
summed, equal weight per event), because prop daily limits apply to the
account, not to one trade.

Usage:
    python -m backtest.run_gap_fade_stops
    python -m backtest.run_gap_fade_stops --calm-vol-max 25 --cost-bps 6
"""
from __future__ import annotations

import argparse

import numpy as np
import pandas as pd

from backtest.run_gap_earnings_split import collect_events, stats
from backtest.run_gap_regime_filter import load_spy_regime
from backtest.run_gap_study import DEFAULT_COST_BPS, DEFAULT_MIN_DOLLAR_VOL, REPORTS_DIR

STOP_GRID_PCT = [1.0, 1.5, 2.0, 3.0, 5.0]  # stop distance below entry (open)


def load_candidate(min_dollar_vol: float, calm_vol_max: float) -> pd.DataFrame:
    events = collect_events(min_dollar_vol)
    cand = events[(events["gap_pct"] <= -2.0) & (events["gap_pct"] > -5.0)
                  & (~events["is_earnings"])].copy()
    regime = load_spy_regime()
    cand = cand.join(regime[["vol20_ann_pct"]], how="inner")
    cand = cand[cand["vol20_ann_pct"] < calm_vol_max]
    return cand


def apply_stop(cand: pd.DataFrame, stop_pct: float | None, cost: float) -> pd.Series:
    """Net PnL per event with an intraday stop at -stop_pct% from the open."""
    raw = cand["oc_ret"]  # long from open to close (fade of a down gap)
    if stop_pct is None:
        return raw - cost
    stop_ret = -stop_pct / 100.0
    stopped = cand["mae"] <= stop_ret
    return pd.Series(np.where(stopped, stop_ret, raw), index=cand.index) - cost


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cost-bps", type=float, default=DEFAULT_COST_BPS)
    ap.add_argument("--min-dollar-vol", type=float, default=DEFAULT_MIN_DOLLAR_VOL)
    ap.add_argument("--calm-vol-max", type=float, default=25.0)
    args = ap.parse_args()
    cost = args.cost_bps / 1e4

    cand = load_candidate(args.min_dollar_vol, args.calm_vol_max)
    print(f"Candidate events (calm, no-news, 2-5% down gap): {len(cand)}")

    # MAE needs the day's low relative to the open -- join it back from raw files.
    from backtest.run_gap_study import load_ticker
    parts = []
    for ticker, grp in cand.groupby("ticker"):
        df = load_ticker(ticker)
        grp = grp.copy()
        grp["mae"] = (df["low"] / df["open"] - 1.0).reindex(grp.index)
        parts.append(grp)
    cand = pd.concat(parts).sort_index().dropna(subset=["mae"])

    print("\n--- Intraday adverse excursion (MAE = low/open - 1) ---")
    q = cand["mae"].quantile([0.5, 0.25, 0.10, 0.05, 0.01])
    print(f"  median {q[0.5]:+.2%}, 25th {q[0.25]:+.2%}, 10th {q[0.10]:+.2%}, "
          f"5th {q[0.05]:+.2%}, 1st {q[0.01]:+.2%}, worst {cand['mae'].min():+.2%}")
    for thr in (-0.02, -0.03, -0.05):
        share = (cand["mae"] <= thr).mean()
        print(f"  trades sinking below {thr:.0%} intraday: {share:.1%}")

    print("\n--- Stop grid (stop assumed filled AT the level; see docstring) ---")
    print(f"{'stop':>8} {'n_stopped':>10} {'mean_bps':>9} {'win%':>6} {'t':>6} "
          f"{'worst_trade':>12} {'worst_day':>10}")
    rows = []
    for stop in [None] + STOP_GRID_PCT:
        net = apply_stop(cand, stop, cost)
        daily = net.groupby(net.index).sum()  # equal 1-unit weight per event
        s = stats(net)
        n_stopped = int((cand["mae"] <= -stop / 100.0).sum()) if stop else 0
        label = "none" if stop is None else f"-{stop:g}%"
        print(f"{label:>8} {n_stopped:>10} {s['mean_bps']:>+9.1f} {s['win_rate']:>6.1%} "
              f"{s['t_stat']:>6.2f} {net.min():>12.2%} {daily.min():>10.2%}")
        rows.append({"stop": label, "n_stopped": n_stopped, **s,
                     "worst_trade": net.min(), "worst_day_units": daily.min()})

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_csv(REPORTS_DIR / "gap_fade_stops_summary.csv", index=False)
    print("\nNote: 'worst_day' sums same-day events at 1 unit each; divide by your")
    print("per-trade size to map onto a prop daily-loss limit.")
    print("Report: reports/gap_fade_stops_summary.csv")
    print("REMINDER: daily-bar stop fills are approximate -- minute data must confirm.")


if __name__ == "__main__":
    main()


