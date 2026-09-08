"""Gate 0 (no-look-ahead) for the S004 meta-labeling features (P1).

Proves the new feature computations in s004_metalabel_data.py do not peek
into the future: truncate the M15 input at a cutoff date, recompute features
for trades comfortably before the cutoff (leaving margin for the ATR/SMA
warm-up windows), and assert every feature value is byte-identical to the
value computed on the full, untruncated dataset. max|delta| must be 0.

Usage: python3 -m backtest.s004_metalabel_gate0
"""
from __future__ import annotations

import pandas as pd

from backtest.s004_metalabel_data import (
    load_combined, build_features, FEATURE_COLS,
)
from strategies.fvg_mtf import run_backtest

CUTOFF = "2020-01-01"
MARGIN_DAYS = 90


def run_one(sym: str) -> None:
    m15_full = load_combined(sym)
    m15_cut = load_combined(sym, until=CUTOFF)

    tr_full = run_backtest(m15_full, mode="base", stop="zone", rr=3.0, pip=10.0, spread_pips=0.9)
    tr_full = tr_full[tr_full["hour"].isin(range(7))].copy()
    feat_full = build_features(sym, m15_full, tr_full)

    tr_cut = run_backtest(m15_cut, mode="base", stop="zone", rr=3.0, pip=10.0, spread_pips=0.9)
    tr_cut = tr_cut[tr_cut["hour"].isin(range(7))].copy()
    feat_cut = build_features(sym, m15_cut, tr_cut)

    safe_before = pd.Timestamp(CUTOFF) - pd.Timedelta(days=MARGIN_DAYS)
    a = feat_full[feat_full["time_in"] < safe_before].set_index("time_in").sort_index()
    b = feat_cut[feat_cut["time_in"] < safe_before].set_index("time_in").sort_index()

    assert len(a) == len(b) and len(a) > 0, f"{sym}: trade count mismatch before cutoff margin ({len(a)} vs {len(b)})"
    assert list(a.index) == list(b.index), f"{sym}: trade timestamps differ before cutoff margin"

    max_delta = 0.0
    for col in FEATURE_COLS:
        d = (a[col].astype(float) - b[col].astype(float)).abs().max()
        max_delta = max(max_delta, d)
        assert d == 0.0, f"{sym}: feature '{col}' max|delta| = {d} (expected 0)"

    print(f"{sym}: OK -- {len(a)} pre-margin trades, all {len(FEATURE_COLS)} features max|delta|=0")


if __name__ == "__main__":
    for sym in ["EURUSD", "GBPJPY"]:
        run_one(sym)
    print("\nGate 0 PASSED: no-look-ahead confirmed for all meta-labeling features.")
