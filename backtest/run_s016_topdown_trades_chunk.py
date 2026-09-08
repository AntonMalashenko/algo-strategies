"""S016 E2 -- dump full per-trade rows (not just summary stats) for cross-instrument
pattern analysis in losing trades. Same chunking pattern as run_s016_topdown_chunk.py
(device bridge call limit ~45s). RR/trend_ma_days fixed to match the sweep in
reports/s016_topdown_sweep.csv -- this is a diagnostic pass on the SAME backtest, not a
new parameter set.

Usage: python3 backtest/run_s016_topdown_trades_chunk.py SYM1 SYM2 ...
Appends rows to reports/s016_topdown_trades.csv (creates with header on first run).
"""
import sys
import os

sys.path.insert(0, os.getcwd())

from backtest.run_fvg import load_m15, SPEC, DEFAULT_SPEC
from strategies.s016_topdown import run_backtest

OUT_PATH = "reports/s016_topdown_trades_v2.csv"
RR = 2.0
TREND_MA_DAYS = 20


def run_one(sym):
    spec = SPEC.get(sym, DEFAULT_SPEC)
    pip_raw, spread = spec["pip_raw"], spec["spread"]
    m15 = load_m15(sym)
    tr = run_backtest(m15, rr=RR, pip=pip_raw, spread_pips=spread, trend_ma_days=TREND_MA_DAYS)
    if len(tr) == 0:
        return tr
    cost_px = spread * pip_raw
    risk = (tr["entry"] - tr["sl"]).abs()
    tr = tr.copy()
    tr["symbol"] = sym
    tr["gross_r"] = tr["r"] + cost_px / risk
    return tr


def main():
    syms = sys.argv[1:]
    if not syms:
        print("usage: run_s016_topdown_trades_chunk.py SYM1 SYM2 ...")
        return
    os.makedirs("reports", exist_ok=True)
    write_header = not os.path.exists(OUT_PATH) or os.path.getsize(OUT_PATH) == 0
    for sym in syms:
        try:
            tr = run_one(sym)
        except Exception as e:
            print(sym, "ERROR", e)
            continue
        if len(tr) == 0:
            print(sym, "no trades")
            continue
        tr.to_csv(OUT_PATH, mode="a", index=False, header=write_header)
        write_header = False
        print(sym, len(tr), "trades appended")


if __name__ == "__main__":
    main()
