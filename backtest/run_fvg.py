"""Grid runner for the H4-FVG / M15-confirmation strategy.

Runs every combination of entry mode x stop mode x RR, splits trades into
in-sample / out-of-sample by time, and prints a comparison table plus a
per-session breakdown. All trades are saved to reports/fvg_trades_<SYM>.csv
for further slicing.

Usage:
    python -m backtest.run_fvg --symbol EURUSD
"""
from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd

from strategies.fvg_mtf import run_backtest, session_of

ROOT = Path(__file__).resolve().parent.parent
MODES = ["base", "shift", "fvg15", "ob"]
STOPS = ["zone", "swing"]
RRS = [1.0, 1.5, 2.0, 3.0]
OOS_FRAC = 0.3

# ejtrader FX files store prices in MT "points" (point = pip/10) regardless
# of symbol, so one FX pip is always 10 raw units. Index files produced by
# scripts/download_indices.py store REAL prices, so their "pip" is one index
# point and the spread is quoted in points. Defaults below are typical retail
# CFD spreads -- adjust to your broker.
DEFAULT_SPEC = dict(pip_raw=10.0, spread=0.9)   # FX pairs
SPEC = {
    "SPX500M":  dict(pip_raw=1.0, spread=0.5),
    "NAS100M":  dict(pip_raw=1.0, spread=1.0),
    "DAX30M":   dict(pip_raw=1.0, spread=1.2),
    "STOXX50M": dict(pip_raw=1.0, spread=1.5),
    "FR40M":    dict(pip_raw=1.0, spread=1.5),
    "US2000M":  dict(pip_raw=1.0, spread=0.4),
    "UK100M":   dict(pip_raw=1.0, spread=1.5),
}


def load_m15(sym: str) -> pd.DataFrame:
    path = ROOT / "data" / "raw" / sym / f"{sym}m15.csv"
    d = pd.read_csv(path)
    d["Date"] = pd.to_datetime(d["Date"])
    d = d.set_index("Date").sort_index()
    for col in ["open", "high", "low", "close"]:
        d[col] = pd.to_numeric(d[col], errors="coerce")
    d = d.rename(columns={"tick_volume": "volume"})
    return d.dropna(subset=["close"])[["open", "high", "low", "close", "volume"]]


def stats(tr: pd.DataFrame) -> dict:
    if len(tr) == 0:
        return dict(n=0, wr=np.nan, avg_r=np.nan, total_r=np.nan, pf=np.nan)
    wins = tr[tr["r"] > 0]["r"]
    losses = tr[tr["r"] <= 0]["r"]
    pf = wins.sum() / abs(losses.sum()) if len(losses) and losses.sum() != 0 else np.inf
    return dict(n=len(tr), wr=(tr["r"] > 0).mean(),
                avg_r=tr["r"].mean(), total_r=tr["r"].sum(), pf=pf)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="EURUSD")
    ap.add_argument("--spread-pips", type=float, default=None,
                    help="override spread (FX pips / index points)")
    args = ap.parse_args()

    sym = args.symbol.upper()
    m15 = load_m15(sym)
    spec = SPEC.get(sym, DEFAULT_SPEC)
    pip = spec["pip_raw"]
    if args.spread_pips is None:
        args.spread_pips = spec["spread"]
    split_time = m15.index[int(len(m15) * (1 - OOS_FRAC))]
    print(f"{sym}: {len(m15)} M15 bars {m15.index.min()}..{m15.index.max()}")
    print(f"IS/OOS split at {split_time}  |  spread {args.spread_pips} pips\n")

    all_trades = []
    rows = []
    for mode in MODES:
        for stop in STOPS:
            if mode == "base" and stop == "swing":
                continue
            for rr in RRS:
                tr = run_backtest(m15, mode=mode, stop=stop, rr=rr,
                                  pip=pip, spread_pips=args.spread_pips)
                if len(tr):
                    tr["session"] = tr["hour"].map(session_of)
                    tr["symbol"] = sym
                    all_trades.append(tr)
                is_tr = tr[tr["time_in"] < split_time] if len(tr) else tr
                oos_tr = tr[tr["time_in"] >= split_time] if len(tr) else tr
                si, so = stats(is_tr), stats(oos_tr)
                rows.append(dict(mode=mode, stop=stop, rr=rr,
                                 n_is=si["n"], wr_is=si["wr"], avgR_is=si["avg_r"],
                                 totR_is=si["total_r"],
                                 n_oos=so["n"], wr_oos=so["wr"], avgR_oos=so["avg_r"],
                                 totR_oos=so["total_r"]))

    table = pd.DataFrame(rows)
    pd.set_option("display.width", 200)
    fmt = table.copy()
    for c in ["wr_is", "wr_oos"]:
        fmt[c] = (fmt[c] * 100).round(1)
    for c in ["avgR_is", "avgR_oos", "totR_is", "totR_oos"]:
        fmt[c] = fmt[c].round(2)
    print(fmt.to_string(index=False))

    out_dir = ROOT / "reports"
    out_dir.mkdir(exist_ok=True)
    if all_trades:
        big = pd.concat(all_trades, ignore_index=True)
        big.to_csv(out_dir / f"fvg_trades_{sym}.csv", index=False)
        print(f"\nSaved {len(big)} trades -> reports/fvg_trades_{sym}.csv")

        print("\n=== Session breakdown (avg R / n), all configs pooled ===")
        pool = big.groupby(["mode", "session"])["r"].agg(["mean", "count"]).round(2)
        print(pool.to_string())


if __name__ == "__main__":
    main()
