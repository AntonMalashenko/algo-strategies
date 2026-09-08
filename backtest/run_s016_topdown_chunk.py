"""S016 E2 (topdown funnel) -- full-universe sweep, run in small chunks through the
device bridge (each `device_bash` call is capped at ~45s, and a single M15-history
backtest already takes 2-12s per instrument depending on FX vs index CFD -- same
chunking pattern as `run_s016_minimal_chunk.py` used for E1).

Usage:  python3 backtest/run_s016_topdown_chunk.py SYM1 SYM2 SYM3 ...

Appends one row per symbol to reports/s016_topdown_sweep.csv (creates the file with a
header on first run). Re-running with the same symbol re-appends a duplicate row --
by design, so a partial/failed sweep can be restarted by just re-listing the symbols
still missing rather than needing idempotency logic; dedupe at analysis time if needed.

v0.7 code (strategies/s016_topdown.py): H1 confirm can be order-block OR indyusment;
stop keyed to H4-zone touch point / far edge, or M15-zone with an 8-pip floor for
indyusment-confirmed trades. RR=2.0, trend_ma_days=20 (fixed, same as the 4-instrument
spot checks in the passport SS5.2) -- not re-optimized here, per Anton's "давай"
2026-08-26: run the sweep on the current design rather than keep tuning on 4 symbols.
"""
import sys
import os
import csv

sys.path.insert(0, os.getcwd())

from backtest.run_fvg import load_m15, SPEC, DEFAULT_SPEC
from strategies.s016_topdown import run_backtest

OUT_PATH = "reports/s016_topdown_sweep_v2.csv"
RR = 2.0
TREND_MA_DAYS = 20


def run_one(sym: str) -> dict:
    spec = SPEC.get(sym, DEFAULT_SPEC)
    pip_raw, spread = spec["pip_raw"], spec["spread"]
    m15 = load_m15(sym)
    tr = run_backtest(m15, rr=RR, pip=pip_raw, spread_pips=spread, trend_ma_days=TREND_MA_DAYS)
    if len(tr) == 0:
        return dict(symbol=sym, n=0, net_avgR="", gross_avgR="", wr="", pip_raw=pip_raw,
                    spread=spread, note="no_trades")
    cost_px = spread * pip_raw
    risk = (tr["entry"] - tr["sl"]).abs()
    gross_r = tr["r"] + cost_px / risk
    return dict(
        symbol=sym, n=len(tr),
        net_avgR=round(tr["r"].mean(), 5), gross_avgR=round(gross_r.mean(), 5),
        wr=round((tr["r"] > 0).mean(), 5),
        pip_raw=pip_raw, spread=spread, note="",
    )


def main():
    syms = sys.argv[1:]
    if not syms:
        print("usage: run_s016_topdown_chunk.py SYM1 SYM2 ...")
        return
    write_header = not os.path.exists(OUT_PATH)
    os.makedirs("reports", exist_ok=True)
    with open(OUT_PATH, "a", newline="") as f:
        fieldnames = ["symbol", "n", "net_avgR", "gross_avgR", "wr", "pip_raw", "spread", "note"]
        w = csv.DictWriter(f, fieldnames=fieldnames)
        if write_header:
            w.writeheader()
        for sym in syms:
            try:
                row = run_one(sym)
            except Exception as e:
                row = dict(symbol=sym, n="", net_avgR="", gross_avgR="", wr="",
                           pip_raw="", spread="", note=f"ERROR: {e}")
            w.writerow(row)
            print(sym, row)


if __name__ == "__main__":
    main()
