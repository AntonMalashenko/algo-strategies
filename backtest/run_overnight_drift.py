"""Runner for the S025 US Index Overnight Drift decomposition/backtest.

Pipeline: daily OHLC (data/raw/<INDEX>/<INDEX>.csv) -> data-quality diagnostic
(is "open" a real cash-session open or a backfilled == prior-close artifact?)
-> overnight/intraday decomposition (strategies/overnight_drift.py) -> equity
curves, per-year breakdown, and a Gate 0 no-look-ahead check, printed side by
side against plain buy-and-hold for the same window.

Usage:
    python -m backtest.run_overnight_drift
    python -m backtest.run_overnight_drift --cost-bps 0.5 --start 2010-01-01
    python -m backtest.run_overnight_drift --no-reliability-clip   # full history, unfiltered

Data note (see strategies/overnight_drift.py module docstring): NASDAQ and
SP500 here are the composite-index daily series already fetched for
S003/S011, a proxy for the project's live CFD instruments (NAS100/US500),
not the exact instrument -- every number below is a proxy-instrument result.

Data-quality note (found while building this runner, see
diagnose_open_data_quality): on both series, "open" is backfilled equal to
the prior close for most of the older history -- NASDAQ before ~2000-07-28,
SP500 before ~1988-05-23 (detector thresholds, inspect around the boundary
before trusting it blindly). By DEFAULT this runner clips the analysis to
start at that detected date, so the printed numbers are not diluted by a
contaminated multi-decade region of manufactured 0.0% overnight returns.
Pass --no-reliability-clip to see the (misleading) full-history numbers.
"""
from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

from strategies.overnight_drift import (
    annual_breakdown,
    buy_and_hold_equity_curve,
    diagnose_open_data_quality,
    gate0_no_look_ahead,
    overnight_equity_curve,
)
from utils.data import load_csv
from utils.report import make_report

ROOT = Path(__file__).resolve().parent.parent
REPORTS_DIR = ROOT / "reports"

INDICES = {
    "NASDAQ": "NASDAQ/NASDAQ.csv",
    "SP500": "SP500/SP500.csv",
}


def run_index(name: str, rel_path: str, start: str | None, cost_bps: float,
              reliability_clip: bool) -> None:
    daily = load_csv(rel_path)

    quality = diagnose_open_data_quality(daily)
    print(f"\n{'=' * 70}\n{name} ({daily.index.min().date()}..{daily.index.max().date()}, "
          f"{len(daily)} days) -- S025 overnight drift\n{'=' * 70}")
    print(f"Data quality: {quality['frac_zero_overnight']:.1%} of ALL rows have "
          f"open == prior close exactly (backfill artifact, not a real overnight "
          f"return); first reliable date (252d rolling contamination < 20%): "
          f"{quality['first_reliable_date']}")

    effective_start = start
    if reliability_clip and quality["first_reliable_date"] is not None:
        detected = str(quality["first_reliable_date"].date())
        if effective_start is None or pd.Timestamp(detected) > pd.Timestamp(effective_start):
            effective_start = detected
        print(f"Clipping analysis window to start {effective_start} "
              f"(pass --no-reliability-clip to see unfiltered full-history numbers)")
    if effective_start:
        daily = daily.loc[effective_start:]

    max_abs_delta = gate0_no_look_ahead(daily)
    print(f"\nGate 0 (no-look-ahead) max|delta| = {max_abs_delta!r} "
          f"({'PASS' if max_abs_delta == 0.0 else 'FAIL'})")
    print(f"Analysis window: {daily.index.min().date()}..{daily.index.max().date()} "
          f"({len(daily)} days)")

    net_ret = overnight_equity_curve(daily, cost_bps_per_side=cost_bps).pct_change().dropna()
    overnight_equity = overnight_equity_curve(daily, cost_bps_per_side=cost_bps, start_equity=10_000.0)
    bnh_equity = buy_and_hold_equity_curve(daily, start_equity=10_000.0)

    print(f"\n-- Overnight-only, net of {cost_bps:.2f} bps/side round-trip cost --")
    make_report(overnight_equity, net_ret, name=f"S025_overnight_{name}",
                periods_per_year=252, reports_dir=REPORTS_DIR)

    print(f"\n-- Buy & hold (close-to-close), same window, for comparison --")
    make_report(bnh_equity, name=f"S025_buyhold_{name}",
                periods_per_year=252, reports_dir=REPORTS_DIR)

    print("\n-- Per-calendar-year overnight vs. intraday mean return / hit rate --")
    breakdown = annual_breakdown(daily)
    with pd.option_context("display.float_format", lambda v: f"{v:.4%}" if abs(v) < 1 else f"{v:.0f}"):
        print(breakdown.to_string())
    positive_years = int((breakdown["overnight_mean"] > 0).sum())
    print(f"\nOvernight mean return positive in {positive_years}/{len(breakdown)} calendar years.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cost-bps", type=float, default=0.0,
                     help="round-trip cost in bps of notional per side (0 = frictionless E2 baseline)")
    ap.add_argument("--start", type=str, default=None,
                     help="restrict to dates on/after this ISO date (combined with the "
                          "reliability clip, whichever start is later wins)")
    ap.add_argument("--no-reliability-clip", dest="reliability_clip", action="store_false",
                     help="disable the automatic open-data-quality clip (see module docstring)")
    args = ap.parse_args()

    for name, rel_path in INDICES.items():
        run_index(name, rel_path, args.start, args.cost_bps, args.reliability_clip)


if __name__ == "__main__":
    main()
