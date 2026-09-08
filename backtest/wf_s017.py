"""S017 Gate 1: walk-forward over the ZigZag/Fibonacci/EMA parameter grid.

Two views, both strictly inside the IS window (the OOS tail from
run_s017.OOS_START is never read here):

1. Full-grid IS table -- every config's total R / trade count / per-year
   consistency, to see whether ANY neighborhood of the grid forms a positive
   plateau (project rule: a plateau, not a lonely peak).
2. Anchored walk-forward -- train on [start .. year Y), pick the best config
   by train total R (min trade count enforced), trade year Y with it, stitch
   all test years. An edge must survive this to pass Gate 1.

Usage:
    python -m backtest.wf_s017 [--symbol US500] [--tf 30min|1h]
"""
from __future__ import annotations

import argparse
import itertools

import pandas as pd

from backtest.run_s017 import is_slice, load_stitched_m15, resample, stats
from strategies.s017_elliott import BASE_S017, run_backtest

# --- parameter grid (Gate 1 axes from the ticket: ZigZag deviation, Fibonacci
# tolerances, EMA period; plus both entry schemes) ---
GRID = dict(
    zz_atr_mult=[1.5, 2.0, 3.0, 4.0, 5.0],
    ema_period=[100, 200, 300],
    wave3_fib_min=[0.0, 1.0, 1.618],
    entry_mode=["abc", "wave4"],
)
WF_FIRST_TEST_YEAR = 2015   # train window is anchored at data start (2005)
MIN_TRAIN_TRADES = 30       # a train pick below this is noise, skip the year
SPREAD_PTS = 0.4            # realistic base spread for all Gate 1 runs


def config_grid():
    keys = list(GRID)
    for combo in itertools.product(*(GRID[k] for k in keys)):
        yield BASE_S017.with_(spread_pts=SPREAD_PTS, **dict(zip(keys, combo)))


def yearly_r(trades: pd.DataFrame) -> pd.Series:
    if len(trades) == 0:
        return pd.Series(dtype=float)
    return trades.set_index("exit_time")["r"].resample("YE").sum()


def full_grid_table(bars: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for cfg in config_grid():
        tr = run_backtest(bars, cfg)
        st = stats(tr)
        yr = yearly_r(tr)
        rows.append(dict(
            zz=cfg.zz_atr_mult, ema=cfg.ema_period, fib3=cfg.wave3_fib_min,
            mode=cfg.entry_mode, **st,
            pos_years=int((yr > 0).sum()), neg_years=int((yr < 0).sum()),
        ))
    return pd.DataFrame(rows).sort_values("total_r", ascending=False)


def anchored_walkforward(bars: pd.DataFrame) -> pd.DataFrame:
    """Pick best-on-train each year, trade the next year with it."""
    grid = list(config_grid())
    # one full-history run per config; slice per year afterwards (engine is
    # causal, so full-run trades restricted to a window equal a windowed run
    # except for positions straddling the boundary -- acceptable here and
    # identical treatment for train and test).
    runs = {i: run_backtest(bars, cfg) for i, cfg in enumerate(grid)}
    years = range(WF_FIRST_TEST_YEAR, bars.index[-1].year + 1)
    out = []
    for y in years:
        t0 = pd.Timestamp(f"{y}-01-01")
        best_i, best_r = None, -1e18
        for i, tr in runs.items():
            if len(tr) == 0:
                continue
            train = tr[tr["exit_time"] < t0]
            if len(train) < MIN_TRAIN_TRADES:
                continue
            r = train["r"].sum()
            if r > best_r:
                best_i, best_r = i, r
        if best_i is None:
            out.append(dict(year=y, picked=None, test_n=0, test_r=0.0))
            continue
        tr = runs[best_i]
        test = tr[(tr["exit_time"] >= t0)
                  & (tr["exit_time"] < pd.Timestamp(f"{y + 1}-01-01"))]
        cfg = grid[best_i]
        out.append(dict(
            year=y, picked=f"zz={cfg.zz_atr_mult} ema={cfg.ema_period} "
                           f"fib3={cfg.wave3_fib_min} {cfg.entry_mode}",
            train_r=round(best_r, 1), test_n=len(test),
            test_r=round(test["r"].sum(), 2)))
    return pd.DataFrame(out)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="US500")
    ap.add_argument("--tf", default="30min", choices=["30min", "1h"])
    args = ap.parse_args()

    bars = is_slice(resample(load_stitched_m15(args.symbol), args.tf))
    print(f"{args.symbol} {args.tf}: {len(bars)} IS bars "
          f"{bars.index.min()} .. {bars.index.max()}\n")

    table = full_grid_table(bars)
    pd.set_option("display.width", 200)
    print("=== Full-grid IS table (net of spread, sorted by total R) ===")
    print(table.to_string(index=False))
    pos = table[table["total_r"] > 0]
    print(f"\npositive configs: {len(pos)}/{len(table)}")

    print("\n=== Anchored walk-forward (best-on-train, trade next year) ===")
    wf = anchored_walkforward(bars)
    print(wf.to_string(index=False))
    print(f"\nWF stitched OOS: n={wf['test_n'].sum()}, "
          f"total R={wf['test_r'].sum():.2f}, "
          f"positive years {int((wf['test_r'] > 0).sum())}/{len(wf)}")


if __name__ == "__main__":
    main()
