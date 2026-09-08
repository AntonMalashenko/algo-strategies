"""Decisive minute-level test for the no-news gap-fade candidate.

Takes the daily-bar candidate events (calm market, no-news, 2-5% down gap --
see docs/HANDOFF_GAP_FADE_RESEARCH.md, experiments E1-E4) that fall inside
the minute-data window and replays each trade on 1-minute bars
(data/raw/<TICKER>/<TICKER>_1m.parquet, RTH only, split-adjusted).

Questions E4 left open, answerable only with minute data:
  1. Delayed entry -- does entering 15/30/60 min after the open (after the
     initial flush) improve the entry enough to allow tighter stops?
  2. Early exit -- is most of the bounce earned by midday, allowing shorter
     exposure?
  3. Sequence-accurate stops -- with real bar ordering, does a stop still
     sell the low, or does it work with a better-timed entry?

Grid: entry at open+{0,15,30,60}min x exit at {12:00, 14:00, close} x stop
{none, -1%, -2%} from the entry price. Stop fill: first minute bar whose low
touches the stop, filled at the stop price (at the bar's open if it gaps
through). Costs 6 bps round-trip per trade.

Caveats: minute window is only ~2 years (Polygon free tier), so this is a
subsample check of E1-E4's 16-year signal, not a re-validation; the daily
candidate list itself is survivorship-biased (see handoff doc).

Usage:
    python -m backtest.run_gap_fade_minute
"""
from __future__ import annotations

import argparse

import numpy as np
import pandas as pd

from backtest.run_gap_fade_stops import load_candidate
from backtest.run_gap_study import DEFAULT_COST_BPS, DEFAULT_MIN_DOLLAR_VOL, REPORTS_DIR
from utils.data import DATA_RAW

ENTRY_DELAYS_MIN = [0, 15, 30, 60]
EXIT_TIMES = ["12:00", "14:00", "15:59"]
STOPS_PCT = [None, 1.0, 2.0]


def load_minute(ticker: str) -> pd.DataFrame | None:
    path = DATA_RAW / ticker / f"{ticker}_1m.parquet"
    if not path.exists():
        return None
    return pd.read_parquet(path)


def replay_trade(day_bars: pd.DataFrame, entry_delay_min: int, exit_time: str,
                 stop_pct: float | None) -> float | None:
    """Return gross trade return, or None if the day lacks usable bars."""
    session_open = day_bars.index[0]
    entry_ts = session_open + pd.Timedelta(minutes=entry_delay_min)
    window = day_bars.loc[day_bars.index >= entry_ts]
    if window.empty:
        return None
    entry_price = window.iloc[0]["open"]

    exit_ts = day_bars.index[0].normalize() + pd.Timedelta(f"{exit_time}:00")
    window = window.loc[window.index <= exit_ts]
    if window.empty:
        return None

    if stop_pct is not None:
        stop_price = entry_price * (1 - stop_pct / 100.0)
        hit = window["low"] <= stop_price
        if hit.any():
            first = window.loc[hit].iloc[0]
            fill = min(first["open"], stop_price)  # gap-through fills worse
            return fill / entry_price - 1.0
    return window.iloc[-1]["close"] / entry_price - 1.0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cost-bps", type=float, default=DEFAULT_COST_BPS)
    ap.add_argument("--min-dollar-vol", type=float, default=DEFAULT_MIN_DOLLAR_VOL)
    ap.add_argument("--calm-vol-max", type=float, default=25.0)
    args = ap.parse_args()
    cost = args.cost_bps / 1e4

    cand = load_candidate(args.min_dollar_vol, args.calm_vol_max)
    minute_cache: dict[str, pd.DataFrame | None] = {}
    rows = []
    n_events = 0
    for (date, ticker), _ in cand.groupby([cand.index, "ticker"]):
        if ticker not in minute_cache:
            minute_cache[ticker] = load_minute(ticker)
        bars = minute_cache[ticker]
        if bars is None:
            continue
        day_key = date.strftime("%Y-%m-%d")
        try:
            day_bars = bars.loc[day_key]
        except KeyError:
            continue
        if len(day_bars) < 100:  # skip half-days / broken sessions
            continue
        n_events += 1
        for delay in ENTRY_DELAYS_MIN:
            for exit_time in EXIT_TIMES:
                for stop in STOPS_PCT:
                    ret = replay_trade(day_bars, delay, exit_time, stop)
                    if ret is not None:
                        rows.append({"date": date, "ticker": ticker,
                                     "delay": delay, "exit": exit_time,
                                     "stop": stop if stop is not None else 0.0,
                                     "net": ret - cost})

    if not rows:
        raise SystemExit("No candidate events overlap the minute-data window.")
    df = pd.DataFrame(rows)
    print(f"Events replayed on minute bars: {n_events} "
          f"({df['date'].min().date()}..{df['date'].max().date()})")

    print("\n--- Net mean bps/trade by entry delay x exit x stop ---")
    print(f"{'delay':>6} {'exit':>6} {'stop':>6} {'n':>5} {'mean':>8} {'win%':>6} {'t':>6}")
    summary_rows = []
    for (delay, exit_time, stop), grp in df.groupby(["delay", "exit", "stop"]):
        r = grp["net"]
        t = r.mean() / (r.std(ddof=1) / np.sqrt(len(r))) if len(r) > 1 else np.nan
        stop_lbl = "none" if stop == 0.0 else f"-{stop:g}%"
        print(f"{delay:>6} {exit_time:>6} {stop_lbl:>6} {len(r):>5} "
              f"{r.mean() * 1e4:>+8.1f} {(r > 0).mean():>6.1%} {t:>6.2f}")
        summary_rows.append({"delay": delay, "exit": exit_time, "stop": stop_lbl,
                             "n": len(r), "mean_bps": r.mean() * 1e4,
                             "win_rate": (r > 0).mean(), "t_stat": t})

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(summary_rows).to_csv(REPORTS_DIR / "gap_fade_minute_grid.csv", index=False)
    df.to_csv(REPORTS_DIR / "gap_fade_minute_trades.csv", index=False)
    print("\nReports: reports/gap_fade_minute_grid.csv, reports/gap_fade_minute_trades.csv")
    print("NOTE: ~2-year subsample; compare the delay-0/close/no-stop cell against")
    print("the daily-bar result for the same window before trusting other cells.")


if __name__ == "__main__":
    main()

