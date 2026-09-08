"""S016 minimal hypothesis (E1) -- chunked runner around run_s016_minimal.py.

Same logic as backtest/run_s016_minimal.py (imported directly, not
duplicated) but sliced into pieces that each finish in well under the
device-bridge shell's per-call time limit (~45s). run_s016_minimal.py's
full grid takes several minutes end to end when run as one process, which
the bridge cannot execute in a single call -- this script assembles the
same result across several short calls, appending to the same reports/
output. Not a replacement for run_s016_minimal.py; delete once the console
session can run the original directly, or keep as a bridge-friendly variant.

Usage:
    python -m backtest.run_s016_minimal_chunk grid <trend_ma_days> <fta|none>
    python -m backtest.run_s016_minimal_chunk baseline
    python -m backtest.run_s016_minimal_chunk champion
    python -m backtest.run_s016_minimal_chunk headline
    python -m backtest.run_s016_minimal_chunk correlate
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

from strategies.fvg_mtf import run_backtest
from backtest.run_s016_minimal import (
    CORE_PAIRS, RR, STOP, MODE, S004_CHAMPION, load_pair, stats, daily_r,
)

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "reports"
OUT.mkdir(exist_ok=True)
GRID_CSV = OUT / "s016_minimal_grid.csv"
HEADLINE_TRADES_CSV = OUT / "s016_minimal_headline_trades.csv"
CHAMPION_TRADES_CSV = OUT / "s016_minimal_champion_trades.csv"


def _pairs():
    return {sym: load_pair(sym) for sym in CORE_PAIRS}


def cmd_grid(tmd_arg: str, fta_arg: str):
    tmd = int(tmd_arg)
    fta = None if fta_arg.lower() == "none" else float(fta_arg)
    pooled = []
    for sym, (m15, pip, spread) in _pairs().items():
        tr = run_backtest(m15, mode=MODE, stop=STOP, rr=RR, pip=pip,
                           spread_pips=spread, trend_ma_days=tmd,
                           trend_align="with", fta_min_r=fta)
        if len(tr):
            tr["symbol"] = sym
            pooled.append(tr)
    big = pd.concat(pooled, ignore_index=True) if pooled else pd.DataFrame()
    s = stats(big)
    row = pd.DataFrame([dict(trend_ma_days=tmd, fta_min_r=fta, **s)])
    header = not GRID_CSV.exists()
    row.to_csv(GRID_CSV, mode="a", header=header, index=False)
    print(f"grid tmd={tmd} fta={fta}: {s}")
    if tmd == 20 and fta == 0.5:
        big.to_csv(HEADLINE_TRADES_CSV, index=False)
        print(f"  saved headline trades -> {HEADLINE_TRADES_CSV} ({len(big)} rows)")


def cmd_baseline():
    pooled = []
    for sym, (m15, pip, spread) in _pairs().items():
        tr = run_backtest(m15, mode=MODE, stop=STOP, rr=RR, pip=pip, spread_pips=spread)
        if len(tr):
            tr["symbol"] = sym
            pooled.append(tr)
    big = pd.concat(pooled, ignore_index=True) if pooled else pd.DataFrame()
    s = stats(big)
    print(f"no-filter baseline: {s}")
    pd.DataFrame([dict(trend_ma_days=None, fta_min_r=None, label="no_filter_baseline", **s)]).to_csv(
        OUT / "s016_minimal_baseline.csv", index=False)


def cmd_champion():
    pooled = []
    for sym, (m15, pip, spread) in _pairs().items():
        tr = run_backtest(m15, pip=pip, spread_pips=spread, **S004_CHAMPION)
        if len(tr):
            tr = tr[tr["hour"].between(0, 6)]
            if len(tr):
                tr["symbol"] = sym
                pooled.append(tr)
    big = pd.concat(pooled, ignore_index=True) if pooled else pd.DataFrame()
    big.to_csv(CHAMPION_TRADES_CSV, index=False)
    s = stats(big)
    print(f"S004 champion (Asia): {s}")


def cmd_headline_details():
    if not HEADLINE_TRADES_CSV.exists():
        print("run `grid 20 0.5` first")
        return
    headline = pd.read_csv(HEADLINE_TRADES_CSV)
    print("=== Per-pair breakdown, headline config trend_ma_days=20 fta_min_r=0.5 ===")
    if len(headline):
        per_pair = headline.groupby("symbol").apply(lambda g: pd.Series(stats(g))).round(4)
        print(per_pair.to_string())
    print()
    print("=== Year-by-year, headline config ===")
    if len(headline):
        yr = headline.copy()
        yr["year"] = pd.to_datetime(yr["time_in"]).dt.year
        per_year = yr.groupby("year").apply(lambda g: pd.Series(stats(g))).round(4)
        print(per_year.to_string())


def cmd_correlate():
    if not HEADLINE_TRADES_CSV.exists() or not CHAMPION_TRADES_CSV.exists():
        print("run `grid 20 0.5` and `champion` first")
        return
    headline = pd.read_csv(HEADLINE_TRADES_CSV)
    champion = pd.read_csv(CHAMPION_TRADES_CSV)
    r_s016 = daily_r(headline)
    r_s004 = daily_r(champion)
    joined = pd.concat([r_s016.rename("s016"), r_s004.rename("s004")], axis=1).fillna(0.0)
    corr = joined["s016"].corr(joined["s004"]) if len(joined) > 1 else float("nan")
    print(f"S016 headline daily-R n={len(r_s016)}  S004 champion daily-R n={len(r_s004)}  "
          f"joined calendar n={len(joined)}  corr={corr:.3f}")


if __name__ == "__main__":
    cmd = sys.argv[1]
    if cmd == "grid":
        cmd_grid(sys.argv[2], sys.argv[3])
    elif cmd == "baseline":
        cmd_baseline()
    elif cmd == "champion":
        cmd_champion()
    elif cmd == "headline":
        cmd_headline_details()
    elif cmd == "correlate":
        cmd_correlate()
    else:
        raise SystemExit(f"unknown command {cmd}")
