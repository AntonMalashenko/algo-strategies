"""S016 minimal hypothesis (E1): bare skeleton edge check on the S004 pipeline.

Reuses strategies/fvg_mtf.py end to end (same H4 FVG zone definition, same
event-driven engine, same IS data/costs already validated for S004) with two
knobs S004 never turns on by default:

  trend_ma_days / trend_align="with"  -- D1-direction filter (already existed
                                          in the engine as an experimental
                                          flag, never used as S004's default).
  fta_min_r                            -- Failure-To-Advance filter, added in
                                          this session (2026-08-26) as a new
                                          off-by-default parameter -- see the
                                          docstring of run_backtest.

This is deliberately NOT the full S016 idea (no order-block/rejection-block
zone type, no indyusment, no H1/M5 confirm, no session restriction) -- it is
the "recommended first step" from Jira ALGODEV-25 / claude/strategy-passport-
S016.md Sec.5: does the bare skeleton have an edge at all, before layering
the subjective filters on top.

IMPORTANT: this script only ever touches the IS window already on disk
(data/raw/<SYM>/<SYM>m15.csv, 2012-11..2022-03 -- ejtrader data). S004's true
OOS (histdata, 2022-2026) is NOT present in this data/raw tree and is never
read here. If S016 ever needs its own true OOS, that is a separate decision
(see passport Sec.6) -- this script does not make it.

Usage:
    python -m backtest.run_s016_minimal
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from strategies.fvg_mtf import run_backtest
from backtest.run_fvg import load_m15, SPEC, DEFAULT_SPEC

ROOT = Path(__file__).resolve().parent.parent

# Core 7 FX pairs S004 was validated on (docs/STRATEGY_S004.md) -- reused
# as-is, not re-chosen, so any edge found here is measured on the same
# instrument set as the strategy this pipeline is proven on.
CORE_PAIRS = ["GBPJPY", "EURUSD", "USDCHF", "GBPUSD", "EURJPY", "USDJPY", "AUDUSD"]

RR = 2.0                                   # fixed take, per passport Sec.5
STOP = "zone"                              # stop behind the zone, per passport Sec.5
MODE = "base"                              # no M15 confirmation, per passport Sec.5
TREND_GRID = [10, 20, 50]                  # D1-direction SMA window, not yet walk-forwarded
FTA_GRID = [None, 0.3, 0.5, 0.7]           # FTA threshold in R; 0.5 is the passport's number

# S004's own validated champion (base/zone/RR3, Asia session only) -- recomputed
# here on the same IS data, purely to correlate against the S016 skeleton below.
# This is NOT a re-validation of S004; it reuses the frozen, unmodified defaults.
S004_CHAMPION = dict(mode="base", stop="zone", rr=3.0)


def stats(tr: pd.DataFrame) -> dict:
    if len(tr) == 0:
        return dict(n=0, wr=np.nan, avg_r=np.nan, total_r=np.nan, pf=np.nan)
    wins = tr[tr["r"] > 0]["r"]
    losses = tr[tr["r"] <= 0]["r"]
    pf = wins.sum() / abs(losses.sum()) if len(losses) and losses.sum() != 0 else np.inf
    return dict(n=len(tr), wr=(tr["r"] > 0).mean(),
                avg_r=tr["r"].mean(), total_r=tr["r"].sum(), pf=pf)


def load_pair(sym: str) -> tuple[pd.DataFrame, float, float]:
    m15 = load_m15(sym)
    spec = SPEC.get(sym, DEFAULT_SPEC)
    return m15, spec["pip_raw"], spec["spread"]


def daily_r(tr: pd.DataFrame) -> pd.Series:
    """Sum of realized R per calendar day of entry -- for correlation only."""
    if len(tr) == 0:
        return pd.Series(dtype=float)
    d = tr.copy()
    d["day"] = pd.to_datetime(d["time_in"]).dt.floor("D")
    return d.groupby("day")["r"].sum()


def main():
    pairs = {sym: load_pair(sym) for sym in CORE_PAIRS}
    for sym, (m15, pip, spread) in pairs.items():
        print(f"{sym}: {len(m15)} M15 bars {m15.index.min()}..{m15.index.max()}")
    print()

    # ---- 1. grid: trend_ma_days x fta_min_r, pooled across the 7 pairs ----
    grid_rows = []
    all_trades_by_cfg: dict[tuple, list[pd.DataFrame]] = {}
    for tmd in TREND_GRID:
        for fta in FTA_GRID:
            pooled = []
            for sym, (m15, pip, spread) in pairs.items():
                tr = run_backtest(m15, mode=MODE, stop=STOP, rr=RR, pip=pip,
                                   spread_pips=spread, trend_ma_days=tmd,
                                   trend_align="with", fta_min_r=fta)
                if len(tr):
                    tr["symbol"] = sym
                    pooled.append(tr)
            big = pd.concat(pooled, ignore_index=True) if pooled else pd.DataFrame()
            s = stats(big)
            grid_rows.append(dict(trend_ma_days=tmd, fta_min_r=fta, **s))
            all_trades_by_cfg[(tmd, fta)] = big

    grid = pd.DataFrame(grid_rows)
    fmt = grid.copy()
    fmt["wr"] = (fmt["wr"] * 100).round(1)
    fmt["avg_r"] = fmt["avg_r"].round(4)
    fmt["total_r"] = fmt["total_r"].round(1)
    fmt["pf"] = fmt["pf"].round(2)
    print("=== Grid: trend_ma_days x fta_min_r, pooled 7 pairs, RR=2, base/zone ===")
    print(fmt.to_string(index=False))
    print()

    # ---- 2. no-filter baseline for reference (same RR/stop/mode, no trend, no FTA) ----
    pooled_base = []
    for sym, (m15, pip, spread) in pairs.items():
        tr = run_backtest(m15, mode=MODE, stop=STOP, rr=RR, pip=pip, spread_pips=spread)
        if len(tr):
            tr["symbol"] = sym
            pooled_base.append(tr)
    big_base = pd.concat(pooled_base, ignore_index=True) if pooled_base else pd.DataFrame()
    s = stats(big_base)
    print(f"=== No-filter reference (RR=2, base/zone, no trend, no FTA), pooled 7 pairs ===")
    print(f"n={s['n']}  wr={s['wr']*100:.1f}%  avgR={s['avg_r']:.4f}  totalR={s['total_r']:.1f}  pf={s['pf']:.2f}")
    print()

    # ---- 3. per-pair breakdown for the passport's headline config (tmd=20, fta=0.5) ----
    headline = all_trades_by_cfg[(20, 0.5)]
    print("=== Per-pair breakdown, headline config trend_ma_days=20 fta_min_r=0.5 ===")
    if len(headline):
        per_pair = headline.groupby("symbol").apply(
            lambda g: pd.Series(stats(g))).round(4)
        print(per_pair.to_string())
    else:
        print("(no trades)")
    print()

    # ---- 4. year-by-year for the headline config, pooled ----
    print("=== Year-by-year, headline config trend_ma_days=20 fta_min_r=0.5, pooled ===")
    if len(headline):
        yr = headline.copy()
        yr["year"] = pd.to_datetime(yr["time_in"]).dt.year
        per_year = yr.groupby("year").apply(lambda g: pd.Series(stats(g))).round(4)
        print(per_year.to_string())
    else:
        print("(no trades)")
    print()

    # ---- 5. correlation with S004's own validated champion (Asia/base/zone/RR3) ----
    pooled_s004 = []
    for sym, (m15, pip, spread) in pairs.items():
        tr = run_backtest(m15, pip=pip, spread_pips=spread, **S004_CHAMPION)
        if len(tr):
            tr = tr[tr["hour"].between(0, 6)]        # Asia session, per S004 passport
            if len(tr):
                tr["symbol"] = sym
                pooled_s004.append(tr)
    big_s004 = pd.concat(pooled_s004, ignore_index=True) if pooled_s004 else pd.DataFrame()

    r_s016 = daily_r(headline)
    r_s004 = daily_r(big_s004)
    joined = pd.concat([r_s016.rename("s016"), r_s004.rename("s004")], axis=1).fillna(0.0)
    corr = joined["s016"].corr(joined["s004"]) if len(joined) > 1 else float("nan")
    print(f"=== Correlation check vs S004 champion (Asia/base/zone/RR3), daily R, overlapping calendar ===")
    print(f"S016 headline daily-R n={len(r_s016)}  S004 champion daily-R n={len(r_s004)}  "
          f"joined calendar n={len(joined)}  corr={corr:.3f}")
    print()

    out_dir = ROOT / "reports"
    out_dir.mkdir(exist_ok=True)
    grid.to_csv(out_dir / "s016_minimal_grid.csv", index=False)
    if len(headline):
        headline.to_csv(out_dir / "s016_minimal_headline_trades.csv", index=False)
    print(f"Saved grid -> reports/s016_minimal_grid.csv"
          + (", trades -> reports/s016_minimal_headline_trades.csv" if len(headline) else ""))


if __name__ == "__main__":
    main()
