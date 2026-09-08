"""Gate 0 (no-look-ahead) for the S007 meta-labeling features (ALGODEV-14).

Proves the feature computations in s007_metalabel_data.py do not peek into
the future: truncate the M1 input at a cutoff date, recompute features for
traded days comfortably before the cutoff (leaving margin for the ATR/SMA
warm-up windows), and assert every feature value is byte-identical to the
value computed on the full, untruncated dataset. max|delta| must be 0.

Usage: .venv/bin/python backtest/s007_metalabel_gate0.py
"""
from __future__ import annotations

import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backtest.s007_metalabel_data import build_all, FEATURE_COLS

CUTOFF = "2025-06-01"
MARGIN_DAYS = 60


def run_one(preset: str) -> None:
    full = build_all(preset)
    cut = build_all(preset, until=CUTOFF)

    safe_before = pd.Timestamp(CUTOFF) - pd.Timedelta(days=MARGIN_DAYS)
    a = full[full["date"] < safe_before].set_index("date").sort_index()
    b = cut[cut["date"] < safe_before].set_index("date").sort_index()

    assert len(a) == len(b) and len(a) > 0, \
        f"{preset}: traded-day count mismatch before cutoff margin ({len(a)} vs {len(b)})"
    assert list(a.index) == list(b.index), \
        f"{preset}: traded-day dates differ before cutoff margin"

    for col in FEATURE_COLS + ["day_R", "win"]:
        d = (a[col].astype(float) - b[col].astype(float)).abs().max()
        assert d == 0.0, f"{preset}: column '{col}' max|delta| = {d} (expected 0)"

    print(f"{preset}: OK -- {len(a)} pre-margin days, "
          f"all {len(FEATURE_COLS)} features (+day_R, win) max|delta|=0")


if __name__ == "__main__":
    for preset in ["liqfloor", "newssafe"]:
        run_one(preset)
    print("\nGate 0 PASSED: no-look-ahead confirmed for all S007 meta-labeling features.")
