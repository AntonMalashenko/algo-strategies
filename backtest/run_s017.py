"""S017 runner: data stitch, Gate 0 (no-repaint / no-look-ahead), baseline IS runs.

Data (decision 2026-08-31, ALGODEV-27): US500 = data/raw/SPX500M M15, stitched
from the old ejtrader file (2005..2019) and the fresh file (2019..2026-06);
cross-checks on NAS100M and DAX30M with the same recipe. Trading TFs: M30/H1
resampled from M15 closed bars.

OOS RESERVE: everything from OOS_START on is *never* read by any tuning run in
this script or wf_s017.py. It stays untouched until a single pre-declared
final run at promotion-decision time (strategy-lifecycle gate discipline).

Usage:
    python -m backtest.run_s017 --gate0          # repaint proof only
    python -m backtest.run_s017                  # Gate 0 + baseline IS grid
"""
from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd

from strategies.s017_elliott import BASE_S017, S017Config, run_backtest, zigzag

ROOT = Path(__file__).resolve().parent.parent

# Instrument -> (old csv, fresh csv). Old file covers the early history; the
# fresh file is authoritative from its own start (sources differ slightly in
# the 2019-2020 overlap; the stitch point is the fresh file's first bar).
DATA_FILES = {
    "US500": ("SPX500M/SPX500Mm15.csv", "SPX500M/SPX500Mm15fresh.csv"),
    "NAS100": ("NAS100M/NAS100Mm15.csv", "NAS100M/NAS100Mm15fresh.csv"),
    "DAX": ("DAX30M/DAX30Mm15.csv", "DAX30M/DAX30Mm15fresh.csv"),
}

OOS_START = "2025-07-01"      # reserved OOS tail: 2025-07-01 .. end of data
GATE0_CUTS = 8                # number of truncation points for the repaint proof

# Round-trip spreads (instrument points) for Gate 2 sensitivity. 0.0 = gross;
# mid values are typical cTrader raw-spread US500 quotes; the high value is a
# stress figure (news/late-session widening).
GATE2_SPREADS_PTS = [0.0, 0.4, 0.9, 1.5]


def load_stitched_m15(sym: str) -> pd.DataFrame:
    old_rel, fresh_rel = DATA_FILES[sym]

    def _read(rel: str) -> pd.DataFrame:
        d = pd.read_csv(ROOT / "data" / "raw" / rel)
        d["Date"] = pd.to_datetime(d["Date"])
        d = d.set_index("Date").sort_index()
        for col in ["open", "high", "low", "close"]:
            d[col] = pd.to_numeric(d[col], errors="coerce")
        return d.dropna(subset=["close"])[["open", "high", "low", "close"]]

    old, fresh = _read(old_rel), _read(fresh_rel)
    stitched = pd.concat([old[old.index < fresh.index.min()], fresh])
    stitched = stitched[~stitched.index.duplicated(keep="last")].sort_index()
    return stitched


def resample(m15: pd.DataFrame, tf: str) -> pd.DataFrame:
    agg = dict(open="first", high="max", low="min", close="last")
    return m15.resample(tf).agg(agg).dropna(subset=["close"])


def is_slice(df: pd.DataFrame) -> pd.DataFrame:
    """In-sample view: everything strictly before the reserved OOS tail."""
    return df[df.index < pd.Timestamp(OOS_START)]


def gate0_repaint_proof(df: pd.DataFrame, cfg: S017Config) -> None:
    """Prove pivots and trades already known at time t never change when the
    future is cut off. max|Δ| must be exactly 0 (project standard)."""
    n = len(df)
    full_pivots = zigzag(df, cfg)
    full_trades = run_backtest(df, cfg)
    cuts = np.linspace(n // 4, n - 1, GATE0_CUTS, dtype=int)
    for cut in cuts:
        part = zigzag(df.iloc[:cut], cfg)
        want = [p for p in full_pivots if p.confirm_idx < cut]
        got = [p for p in part if p.confirm_idx < cut]
        assert want == got, (
            f"REPAINT at cut={cut}: confirmed pivots differ "
            f"({len(want)} vs {len(got)})")
        pt = run_backtest(df, cfg, end_i=int(cut))
        if len(full_trades) == 0:
            assert len(pt) == 0
            continue
        want_t = full_trades[full_trades["exit_i"] < cut].reset_index(drop=True)
        got_t = pt[pt["exit_i"] < cut].reset_index(drop=True)
        pd.testing.assert_frame_equal(want_t, got_t)
    print(f"Gate 0 OK: {GATE0_CUTS} truncation points, max|Δ| = 0 "
          f"(pivots and closed trades identical), n_bars={n}, "
          f"n_trades_full={len(full_trades)}")


def stats(tr: pd.DataFrame) -> dict:
    if len(tr) == 0:
        return dict(n=0, wr=np.nan, avg_r=np.nan, total_r=np.nan, pf=np.nan)
    wins, losses = tr[tr["r"] > 0]["r"], tr[tr["r"] <= 0]["r"]
    pf = wins.sum() / abs(losses.sum()) if len(losses) and losses.sum() != 0 else np.inf
    return dict(n=len(tr), wr=round((tr["r"] > 0).mean(), 3),
                avg_r=round(tr["r"].mean(), 3), total_r=round(tr["r"].sum(), 1),
                pf=round(pf, 2))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="US500", choices=sorted(DATA_FILES))
    ap.add_argument("--gate0", action="store_true", help="repaint proof only")
    args = ap.parse_args()

    m15 = load_stitched_m15(args.symbol)
    print(f"{args.symbol} M15 stitched: {m15.index.min()} .. {m15.index.max()} "
          f"({len(m15)} bars); IS ends {OOS_START} (OOS tail reserved)")

    for tf in ["30min", "1h"]:
        bars = is_slice(resample(m15, tf))
        print(f"\n=== {args.symbol} {tf}: {len(bars)} IS bars ===")
        gate0_repaint_proof(bars, BASE_S017)
        if args.gate0:
            continue
        for mode in ["abc", "wave4"]:
            for spread in GATE2_SPREADS_PTS:
                cfg = BASE_S017.with_(entry_mode=mode, spread_pts=spread)
                tr = run_backtest(bars, cfg)
                print(f"  mode={mode:5s} spread={spread:>4} -> {stats(tr)}")


if __name__ == "__main__":
    main()
