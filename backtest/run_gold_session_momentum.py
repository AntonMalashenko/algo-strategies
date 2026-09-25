"""Gate 0 proof-of-life + first honest read of the edge for S024 -- Gold Session
Momentum, XAU/USD M15, 07:00-17:00 UTC (strategy-lifecycle skill sec "validation
gates"; spec claude/strategy-spec-S022-S024-multifactor-portfolio.md sec 3,
engine strategies/gold_session_momentum.py).

This is NOT the Gate 3 prop-firm Monte Carlo (see run_s021_propscheme.py for that
shape) -- it is the first pass that answers "does the frozen spec, run once on
real data with no parameter search, show any edge at all, is it stable across
years, and does it survive realistic gold CFD costs":

* Data: histdata.com XAUUSD M1 (the actual instrument, not a proxy), loaded and
  shifted to true UTC by utils.histdata.load_histdata_m1_utc, resampled to M15
  (label=left). EMA200 needs 200 M15 bars (~2 trading days) of warm-up at the
  very start of the file, so the first possible trade is a few days in -- the
  2022 row starts marginally late, nothing else is lost.
* Config: GOLD_MOMENTUM_BASE only, exactly as specified (Donchian 20, EMA 200,
  ATR 14, expansion window 20 with k=1.0, SL 1.5xATR, TP 2.5xATR). No
  parameter is swept or tuned here, on purpose: this project already has one
  Donchian/EMA200-filter strategy (S017) that looked fine on its "reasonable"
  parameters and then failed walk-forward -- the first read must be the
  untouched spec.
* R-multiple: R = net_price_move / stop_distance, stop_distance = 1.5 x ATR(14)
  at the signal bar (same convention as strategies/orb_intraday and S022).
  A full TP win is +1.667R gross; break-even win rate ignoring time exits and
  costs is 1.5/(1.5+2.5) = 37.5%.
* Walk-forward by calendar year (Gate 1 requirement in this project: positive in
  EVERY year). Reported for every year in the data, including partial 2026
  (histdata ends 2026-06), no year dropped.
* Cost sensitivity (Gate 2). Gold M15 ATR is only a few USD, so the stop is
  typically $2-10/oz -- a fixed round-trip cost of a few tens of cents is a
  meaningful fraction of 1R. Scenarios:
    - 0 bp gross (reference only),
    - 1 bp of entry price (the engine's frozen nominal, ~$0.18/oz at $1800 and
      ~$0.45/oz at $4500 -- tight-spread / raw-ECN assumption),
    - 3 bp and 5 bp (stressed),
    - fixed $0.30/oz round-trip: a typical XAU/USD CFD all-in figure in London/NY
      hours (raw spread ~$0.10-0.20 + ~$5-7 commission per 100-oz lot round trip,
      i.e. ~$0.05-0.07/oz; standard/prop-firm accounts quote ~$0.25-0.40 spread
      with no commission),
    - fixed $0.60/oz round-trip: stressed (wider spread around the London open /
      US data releases, plus some adverse slippage on momentum-bar fills).
  These are assumptions, not a measured broker figure -- no real XAU/USD spread
  has been measured for this project yet.

KNOWN OPTIMISTIC BIASES (not corrected here, see the engine docstring): entry
fills at the breakout bar's own close (a live bot gets bar i+1's open at best);
SL/TP fill exactly at their level (no gap-through slippage).

Usage: python3 backtest/run_gold_session_momentum.py [--trades-csv PATH]
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from strategies.gold_session_momentum import (GOLD_MOMENTUM_BASE, GoldMomentumConfig,
                                              add_indicators, simulate, trades_to_frame)
from utils.histdata import load_histdata_m1_utc, resample_ohlc

DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "histdata"
SYMBOL = "XAUUSD"
BAR_RULE = "15min"

COST_SCENARIOS: list[tuple[str, GoldMomentumConfig]] = [
    ("gross 0bp", GOLD_MOMENTUM_BASE.with_(cost_bps_roundtrip=0.0)),
    ("1bp (base)", GOLD_MOMENTUM_BASE),
    ("3bp", GOLD_MOMENTUM_BASE.with_(cost_bps_roundtrip=3.0)),
    ("5bp", GOLD_MOMENTUM_BASE.with_(cost_bps_roundtrip=5.0)),
    ("$0.30/oz", GOLD_MOMENTUM_BASE.with_(cost_bps_roundtrip=0.0, cost_price_roundtrip=0.30)),
    ("$0.60/oz", GOLD_MOMENTUM_BASE.with_(cost_bps_roundtrip=0.0, cost_price_roundtrip=0.60)),
]


def max_drawdown_r(r: np.ndarray) -> float:
    """Largest peak-to-trough drop of the cumulative R curve (trade sequence)."""
    if len(r) == 0:
        return 0.0
    cum = np.cumsum(r)
    peak = np.maximum.accumulate(np.concatenate([[0.0], cum]))[1:]
    return float((cum - peak).min())


def longest_losing_streak(r: np.ndarray) -> int:
    streak = best = 0
    for x in r:
        streak = streak + 1 if x < 0 else 0
        best = max(best, streak)
    return best


def summarize(tr: pd.DataFrame) -> dict:
    r = tr["r_multiple"].to_numpy()
    return dict(n=len(tr), win=float((r > 0).mean()) if len(r) else np.nan,
                mean_r=float(r.mean()) if len(r) else np.nan,
                median_r=float(np.median(r)) if len(r) else np.nan,
                sum_r=float(r.sum()), max_dd_r=max_drawdown_r(r),
                streak=longest_losing_streak(r))


def by_year(tr: pd.DataFrame) -> pd.DataFrame:
    g = tr.groupby(tr["day"].dt.year)
    out = pd.DataFrame({
        "n": g.size(),
        "win%": 100 * g["r_multiple"].apply(lambda s: (s > 0).mean()),
        "mean_R": g["r_multiple"].mean(),
        "sum_R": g["r_multiple"].sum(),
        "tp%": 100 * g["exit_reason"].apply(lambda s: (s == "tp").mean()),
        "stop_$": g["stop_dist"].mean(),
        "px": g["entry_price"].mean(),
    })
    out.index.name = "year"
    return out


def signal_funnel(m15: pd.DataFrame) -> None:
    ind = add_indicators(m15, GOLD_MOMENTUM_BASE)
    s = ind[ind["in_session"]]
    for side in ("long", "short"):
        brk = s[f"breakout_{side}"]
        trend = brk & s[f"trend_{side}"]
        full = s[f"{side}_signal"]
        print(f"  {side:5s}: in-session Donchian breakouts {int(brk.sum()):6d}  "
              f"+EMA200 side {int(trend.sum()):6d}  +ATR expanding {int(full.sum()):6d}")
    days_with_session = s["day"].nunique()
    days_with_signal = s.loc[s["long_signal"] | s["short_signal"], "day"].nunique()
    print(f"  UTC days with >=1 in-session bar: {days_with_session}; "
          f"days with >=1 valid signal: {days_with_signal}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--trades-csv", type=Path, default=None,
                    help="optional path to write the base-config trade list")
    args = ap.parse_args()

    m1 = load_histdata_m1_utc(SYMBOL, DATA_DIR)
    m15 = resample_ohlc(m1, BAR_RULE)
    print(f"{SYMBOL} M1 (UTC): {m1.index.min()} .. {m1.index.max()} ({len(m1)} bars) "
          f"-> M15 {len(m15)} bars")
    ind = add_indicators(m15, GOLD_MOMENTUM_BASE)
    ready = ind[["donchian_high", "ema", "atr_mean"]].dropna().index
    print(f"first bar with all indicators warmed up: {ready.min()} "
          f"(EMA{GOLD_MOMENTUM_BASE.ema_period} warm-up)\n")

    print("Signal funnel (base config, in-session bars only):")
    signal_funnel(m15)

    base = trades_to_frame(simulate(m15, GOLD_MOMENTUM_BASE))
    if args.trades_csv is not None:
        base.to_csv(args.trades_csv, index=False)
        print(f"trades written to {args.trades_csv}")

    s = summarize(base)
    print(f"\n=== BASE (1bp round-trip) === trades {s['n']}  win {100 * s['win']:.1f}%  "
          f"mean R {s['mean_r']:+.4f}  median R {s['median_r']:+.3f}  sum R {s['sum_r']:+.1f}  "
          f"maxDD {s['max_dd_r']:.1f}R  longest losing streak {s['streak']}")
    print(f"  first trade {base['day'].min().date()}, last trade {base['day'].max().date()}")
    print("  exits:", base["exit_reason"].value_counts().to_dict())
    for d, g in base.groupby("direction"):
        print(f"  {d:5s}: n {len(g):4d}  win {100 * (g['r_multiple'] > 0).mean():5.1f}%  "
              f"mean R {g['r_multiple'].mean():+.4f}")
    for reason, g in base.groupby("exit_reason"):
        print(f"  exit={reason:4s}: n {len(g):4d}  mean R {g['r_multiple'].mean():+.3f}")
    cost_r = (base["gross"] - base["net"]) / base["stop_dist"]
    print(f"  stop distance: median ${base['stop_dist'].median():.2f}/oz; "
          f"1bp cost = median {cost_r.median():.3f}R per trade")

    print("\nWalk-forward by calendar year (base, 1bp):")
    print(by_year(base).to_string(float_format=lambda x: f"{x:9.3f}"))

    print("\nCost sensitivity (Gate 2) -- mean R per trade, overall and by year:")
    rows = {}
    for label, cfg in COST_SCENARIOS:
        tr = trades_to_frame(simulate(m15, cfg))
        yr = tr.groupby(tr["day"].dt.year)["r_multiple"].mean()
        st = summarize(tr)
        row = {"n": st["n"], "win%": 100 * st["win"], "mean_R": st["mean_r"],
               "sum_R": st["sum_r"], "maxDD_R": st["max_dd_r"],
               "pos_years": f"{int((yr > 0).sum())}/{len(yr)}"}
        row.update({str(y): v for y, v in yr.items()})
        rows[label] = row
    print(pd.DataFrame(rows).T.to_string(
        float_format=lambda x: f"{x:+8.3f}" if abs(x) < 100 else f"{x:8.1f}"))


if __name__ == "__main__":
    main()
