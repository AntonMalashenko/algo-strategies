"""Gate 0/1/2 first-pass backtest for S023 -- US Index Breakout, NAS100 (USTEC)
proxy NSXUSD, M15 (strategy-lifecycle skill sec "validation gates"; spec in
claude/strategies-registry.md S023, rules and judgment calls documented in
strategies/us_index_breakout.py's module docstring).

This is a proof-of-life + honest first read of the edge, NOT the prop-firm
Monte Carlo (Gate 3 simulation, see run_s021_propscheme.py for that shape) --
that comes later, and only if the numbers here justify it.

What it reports, all on the frozen US_INDEX_BREAKOUT_BASE config (no tuning):
  1. Headline: trade count, win rate, mean/median R, exit-reason and
     long/short split, ATR-cap binding rate, entry-bar stop-outs, gap-through
     entries (bar opened beyond the stop level -- the idealized stop-price
     fill is optimistic there).
  2. Walk-forward by calendar year (Gate 1 = positive in every year): trade
     count and mean R per year, reported as-is.
  3. Cost sensitivity (Gate 2): the whole run repeated at several round-trip
     costs. Rationale for the grid: at NAS100 ~15-25k, 1bp ~ 1.5-2.5 index
     points, roughly a tight raw-spread USTEC quote (ECN brokers quote ~1pt
     plus commission) -- the NOMINAL case. Prop-firm/retail USTEC spreads are
     typically 1.5-3pt, and a resting stop that fills INTO a breakout (most of
     S023's fills land in the US-open bar) adds slippage on top, so 3bp
     (~5-7pt round trip) is the REALISTIC case used for the Gate 2 verdict.
     5bp is a stress tail (matches the stress point S021's passport used).
     0bp is shown only as the gross reference.
  4. Diagnostics, clearly labelled as NOT the engine's rule and NOT proposed
     variants: (a) an M1 intra-bar re-grade of each trade's exit path (same
     entry/stop/TP; exits re-walked minute by minute from the actual fill
     minute) to measure how much of the result is the M15 engine's
     conservative "stop checked on the entry bar" ordering vs the strategy
     itself; (b) the alternative literal reading of the ambiguous SL clause
     (pure opposite-range-boundary stop, no ATR cap) -- interpretation
     sensitivity only, so Anton can see what the other reading implies.
  5. Mechanical/empirical overlap with S021 (strategies/orb_intraday, used
     read-only): same-date co-firing, direction agreement, and correlation of
     per-day R on the days both trade (S021 day key = fixed-EST date, whose
     09:30-15:59 session is 14:30-20:59 UTC, so it is the same calendar date
     as S023's UTC date).

R-multiple accounting: R = net_price_move / stop_distance per trade (same as
strategies/orb_intraday / run_s021_propscheme.py).

Usage: python3 backtest/run_us_index_breakout.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from strategies.us_index_breakout import (US_INDEX_BREAKOUT_BASE, UsIndexBreakoutConfig,
                                          simulate, trades_to_frame)
from utils.histdata import load_histdata_m1_utc, resample_ohlc

DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "histdata"
SYMBOL = "NSXUSD"            # NAS100 CFD proxy, same source S021 uses
BAR_RULE = "15min"

COST_GRID_BPS = [0.0, 1.0, 3.0, 5.0]   # gross ref / nominal / realistic (Gate 2 verdict) / stress
REALISTIC_COST_BPS = 3.0


def summarize(tr: pd.DataFrame) -> dict:
    r = tr["r_multiple"]
    return dict(n=len(tr), win_rate=float((r > 0).mean()), mean_r=float(r.mean()),
                median_r=float(r.median()), total_r=float(r.sum()))


def by_year(tr: pd.DataFrame, col: str = "r_multiple") -> pd.DataFrame:
    g = tr.groupby(tr["day"].dt.year)[col]
    return pd.DataFrame({"n": g.size(), "win_rate": g.apply(lambda s: (s > 0).mean()),
                         "mean_r": g.mean(), "total_r": g.sum()})


def gap_through_mask(tr: pd.DataFrame, m15: pd.DataFrame) -> np.ndarray:
    """True where the entry bar OPENED at/beyond the stop level (idealized fill is optimistic)."""
    opens = m15["open"].reindex(tr["entry_time"]).to_numpy()
    is_long = tr["direction"].eq("long").to_numpy()
    px = tr["entry_price"].to_numpy()
    return np.where(is_long, opens >= px, opens <= px)


def regrade_on_m1(tr: pd.DataFrame, m1: pd.DataFrame, cfg: UsIndexBreakoutConfig) -> pd.Series:
    """Diagnostic only: re-walk each trade's exit on M1 bars starting from the
    first minute that actually reached the stop level (same entry/stop/TP as the
    M15 engine, same conservative stop-first ordering within any single minute,
    same forced time exit at the last bar before entry_end). Returns net R."""
    out = []
    end_off = pd.Timedelta(hours=cfg.entry_end.hour, minutes=cfg.entry_end.minute)
    for t in tr.itertuples(index=False):
        path = m1.loc[(m1.index >= t.entry_time) & (m1.index < t.day + end_off)]
        hi, lo, cl = (path[c].to_numpy() for c in ("high", "low", "close"))
        is_long = t.direction == "long"
        touched = hi >= t.entry_price if is_long else lo <= t.entry_price
        reached = np.flatnonzero(touched)
        f = int(reached[0])
        exit_px = cl[-1]
        for j in range(f, len(path)):
            stop_hit = lo[j] <= t.stop_price if is_long else hi[j] >= t.stop_price
            tp_hit = hi[j] >= t.tp_price if is_long else lo[j] <= t.tp_price
            if stop_hit:
                exit_px = t.stop_price
                break
            if tp_hit:
                exit_px = t.tp_price
                break
        gross = (exit_px - t.entry_price) if is_long else (t.entry_price - exit_px)
        net = gross - t.entry_price * cfg.cost_bps_roundtrip / 10_000.0
        out.append(net / t.stop_dist)
    return pd.Series(out, index=tr.index)


def s021_trades() -> pd.DataFrame:
    """S021 base-config trades (read-only import of strategies/orb_intraday), with R."""
    from strategies.orb_intraday.config import ORB_BASE
    from strategies.orb_intraday.engine import load_nsxusd_m1, simulate as s021_simulate
    from strategies.orb_intraday.engine import trades_to_frame as s021_frame

    s21 = s021_frame(s021_simulate(load_nsxusd_m1(DATA_DIR), ORB_BASE))
    s21["r_multiple"] = s21["net_pts"] / (ORB_BASE.stop_adr_mult * s21["adr14"])
    return s21


def s021_overlap(tr: pd.DataFrame, s21: pd.DataFrame) -> None:
    """Empirical Gate 3 overlap check against S021 on the same calendar dates."""
    a = tr.set_index("day")[["direction", "r_multiple"]]
    b = s21.set_index("day")[["direction", "r_multiple"]]
    both = a.join(b, how="inner", lsuffix="_s023", rsuffix="_s021")
    same_dir = (both["direction_s023"] == both["direction_s021"])
    first_date, last_date = a.index.min(), a.index.max()
    b_in = b.loc[(b.index >= first_date) & (b.index <= last_date)]
    print(f"S021 trades (same data span): {len(b_in)}   S023 trades: {len(a)}   "
          f"same-date co-fires: {len(both)} "
          f"({len(both) / len(a):.1%} of S023 days, {len(both) / len(b_in):.1%} of S021 days)")
    print(f"  direction agreement on co-fire days: {same_dir.mean():.1%}")
    corr = both["r_multiple_s023"].corr(both["r_multiple_s021"])
    print(f"  per-day R correlation on co-fire days (Pearson): {corr:+.3f}")
    for label, mask in (("same direction", same_dir), ("opposite direction", ~same_dir)):
        sub = both[mask]
        print(f"  {label:18s} n={len(sub):4d}  mean R S023 {sub['r_multiple_s023'].mean():+.3f}"
              f"  S021 {sub['r_multiple_s021'].mean():+.3f}")
    # Daily R series over the union of days (0 on no-trade days) -- portfolio-level co-movement.
    union = a.index.union(b_in.index)
    ra = a["r_multiple"].reindex(union, fill_value=0.0)
    rb = b_in["r_multiple"].reindex(union, fill_value=0.0)
    print(f"  daily-R correlation over the union of trade days (0 = no trade): {ra.corr(rb):+.3f}")


def main() -> None:
    m1 = load_histdata_m1_utc(SYMBOL, DATA_DIR)
    m15 = resample_ohlc(m1, BAR_RULE)
    print(f"{SYMBOL} M1 (UTC): {m1.index.min()} .. {m1.index.max()} ({len(m1)} bars) -> "
          f"{len(m15)} M15 bars")
    cfg = US_INDEX_BREAKOUT_BASE
    print(f"config: {cfg}\n")

    tr = trades_to_frame(simulate(m15, cfg))
    s = summarize(tr)
    print(f"=== BASE ({cfg.cost_bps_roundtrip}bp) === trades {s['n']}  win {s['win_rate']:.1%}  "
          f"mean R {s['mean_r']:+.3f}  median R {s['median_r']:+.3f}  total R {s['total_r']:+.1f}")
    print(f"  exit reasons: {tr['exit_reason'].value_counts().to_dict()}")
    for d, sub in tr.groupby("direction"):
        print(f"  {d:5s} n={len(sub):4d}  win {(sub['r_multiple'] > 0).mean():.1%}  "
              f"mean R {sub['r_multiple'].mean():+.3f}")
    entry_bar_stop = (tr["exit_time"] == tr["entry_time"]) & tr["exit_reason"].eq("stop")
    print(f"  ATR cap binding: {tr['atr_capped'].mean():.1%} of trades  "
          f"(median raw range-stop {tr['raw_stop_dist'].median():.1f}pt vs "
          f"median ATR-capped stop {tr['stop_dist'].median():.1f}pt)")
    print(f"  stopped out on the entry bar itself (M15 conservative ordering): "
          f"{int(entry_bar_stop.sum())} ({entry_bar_stop.mean():.1%})")
    gap = gap_through_mask(tr, m15)
    print(f"  gap-through entries (bar opened beyond the level): {int(gap.sum())}")
    cost_r = (tr["entry_price"] * cfg.cost_bps_roundtrip / 10_000.0) / tr["stop_dist"]
    print(f"  cost per trade in R at {cfg.cost_bps_roundtrip}bp: median {cost_r.median():.3f}R")
    print(f"  entry time-of-day (UTC) top 5: "
          f"{tr['entry_time'].dt.strftime('%H:%M').value_counts().head(5).to_dict()}\n")

    print("=== WALK-FORWARD BY YEAR (base, 1.0bp) ===")
    print(by_year(tr).to_string(float_format=lambda x: f"{x:+.3f}"))
    yr = by_year(tr)
    print(f"  positive years: {(yr['mean_r'] > 0).sum()}/{len(yr)}\n")

    print("=== COST SENSITIVITY (Gate 2) ===")
    for bps in COST_GRID_BPS:
        t2 = trades_to_frame(simulate(m15, cfg.with_(cost_bps_roundtrip=bps)))
        s2 = summarize(t2)
        y2 = by_year(t2)
        tag = "  <- realistic (Gate 2)" if bps == REALISTIC_COST_BPS else ""
        print(f"  {bps:3.1f}bp  n {s2['n']}  win {s2['win_rate']:.1%}  mean R {s2['mean_r']:+.3f}  "
              f"total R {s2['total_r']:+.1f}  "
              f"positive years {(y2['mean_r'] > 0).sum()}/{len(y2)}{tag}")
    print()

    print("=== DIAGNOSTIC (a): M1 intra-bar re-grade of exits (NOT the engine rule) ===")
    for bps in (1.0, REALISTIC_COST_BPS):
        c2 = cfg.with_(cost_bps_roundtrip=bps)
        t2 = trades_to_frame(simulate(m15, c2))
        t2["r_m1"] = regrade_on_m1(t2, m1, c2)
        y2 = by_year(t2, "r_m1")
        print(f"  {bps:3.1f}bp  n {len(t2)}  win {(t2['r_m1'] > 0).mean():.1%}  "
              f"mean R {t2['r_m1'].mean():+.3f}  total R {t2['r_m1'].sum():+.1f}  "
              f"positive years {(y2['mean_r'] > 0).sum()}/{len(y2)}")
        if bps == 1.0:
            print(y2.to_string(float_format=lambda x: f"{x:+.3f}"))
    print()

    print("=== DIAGNOSTIC (b): alternative SL reading, no ATR cap (interpretation "
          "sensitivity, NOT a proposed variant) ===")
    for bps in (1.0, REALISTIC_COST_BPS):
        t3 = trades_to_frame(simulate(m15, cfg.with_(atr_cap_mult=np.inf,
                                                      cost_bps_roundtrip=bps)))
        s3 = summarize(t3)
        y3 = by_year(t3)
        print(f"  {bps:3.1f}bp  n {s3['n']}  win {s3['win_rate']:.1%}  mean R {s3['mean_r']:+.3f}  "
              f"total R {s3['total_r']:+.1f}  positive years {(y3['mean_r'] > 0).sum()}/{len(y3)}  "
              f"exits {t3['exit_reason'].value_counts().to_dict()}")
    print()

    print("=== S021 OVERLAP (Gate 3, empirical; S021 base config, read-only) ===")
    s21 = s021_trades()
    print("-- S023 base (ATR-capped stop):")
    s021_overlap(tr, s21)
    print("-- S023 alternative SL reading (no ATR cap) -- for reference only:")
    s021_overlap(trades_to_frame(simulate(m15, cfg.with_(atr_cap_mult=np.inf))), s21)


if __name__ == "__main__":
    main()
