"""Runner for the S027 US Sector ETF Cross-Sectional Momentum backtest.

Pipeline: 11 SPDR sector ETF daily closes (data/raw/<TICKER>/<TICKER>_1d.csv,
fetched via utils.data.load_yf) -> monthly-rebalance long-only momentum ranking
(strategies/xsect_equity_momentum.run_backtest -- the version the Confluence
page actually specifies) -> equity curve, per-year breakdown, Gate 0 check.

Usage:
    python -m backtest.run_xsect_equity_momentum
    python -m backtest.run_xsect_equity_momentum --cost-bps 1.0 5.0 10.0
    python -m backtest.run_xsect_equity_momentum --daily-variant   # exploratory S012-style reuse instead

Data note: XLRE exists since 2015-10-08, XLC since 2018-06-19 (real SPDR launch
dates). Before those dates the universe is naturally 10 / 9 names; min_universe
keeps thin days from trading. Default run uses the full history from
2000-01-03 (when the other 9 sectors already existed).
"""
from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

from strategies.xsect_equity_momentum import (
    TRADING_DAYS_PER_YEAR,
    XSectEquityMomentumConfig,
    gate0_no_look_ahead,
    gate0_no_look_ahead_daily_variant,
    load_sector_panel,
    run_backtest,
    run_backtest_dollar_neutral_daily_variant,
)
from utils.report import make_report

ROOT = Path(__file__).resolve().parent.parent
REPORTS_DIR = ROOT / "reports"


def run_scenario(close: pd.DataFrame, cfg: XSectEquityMomentumConfig, label: str,
                  daily_variant: bool) -> pd.DataFrame:
    runner = run_backtest_dollar_neutral_daily_variant if daily_variant else run_backtest
    out, w = runner(close, cfg)
    out = out.dropna(subset=["net_ret"])
    print(f"\n{'=' * 70}\n{label} ({out.index.min().date()}..{out.index.max().date()}, "
          f"{len(out)} days)\n{'=' * 70}")

    equity = (1.0 + out["net_ret"]).cumprod() * 10_000.0
    make_report(equity, out["net_ret"], name=f"S027_{label}",
                periods_per_year=TRADING_DAYS_PER_YEAR, reports_dir=REPORTS_DIR)

    breakdown = out.groupby(out.index.year).agg(
        mean_net_ret=("net_ret", "mean"),
        hit_rate=("net_ret", lambda s: float((s > 0).mean())),
        mean_n_pos=("n_pos", "mean"),
        mean_turnover=("turnover", "mean"),
    )
    breakdown.index.name = "year"
    with pd.option_context("display.float_format", lambda v: f"{v:.4%}" if abs(v) < 1 else f"{v:.2f}"):
        print(breakdown.to_string())
    positive_years = int((breakdown["mean_net_ret"] > 0).sum())
    print(f"\nPositive mean daily net return in {positive_years}/{len(breakdown)} calendar years.")
    return out


def run_benchmark(close: pd.DataFrame) -> None:
    """Naive equal-weight-all-11-sectors, rebalanced daily, no ranking at all --
    the benchmark that actually answers 'does the momentum SELECTION add anything
    beyond plain diversified US sector beta', which CAGR/Sharpe on the momentum
    book alone cannot answer by itself."""
    ret = close.pct_change().mean(axis=1, skipna=True).dropna()
    equity = (1.0 + ret).cumprod() * 10_000.0
    print(f"\n{'=' * 70}\nBenchmark: equal-weight ALL {close.shape[1]} sectors, no ranking, "
          f"daily rebalance\n{'=' * 70}")
    make_report(equity, ret, name="S027_benchmark_ew_all_sectors",
                periods_per_year=TRADING_DAYS_PER_YEAR, reports_dir=REPORTS_DIR)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cost-bps", type=float, nargs="+", default=[0.0],
                     help="one or more round-trip costs in bps of notional per side "
                          "(0.0 = frictionless E2 baseline; pass several for an E3 sweep)")
    ap.add_argument("--start", type=str, default=None,
                     help="restrict to dates on/after this ISO date")
    ap.add_argument("--daily-variant", action="store_true",
                     help="run the exploratory daily-rebalance dollar-neutral variant "
                          "instead of the spec'd monthly long-only book")
    args = ap.parse_args()

    close = load_sector_panel()
    if args.start:
        close = close.loc[args.start:]

    base_cfg = XSectEquityMomentumConfig()
    gate0_fn = gate0_no_look_ahead_daily_variant if args.daily_variant else gate0_no_look_ahead
    max_abs_delta = gate0_fn(close, base_cfg)
    print(f"Variant: {'daily dollar-neutral (exploratory)' if args.daily_variant else 'monthly long-only (spec)'}")
    print(f"Gate 0 (no-look-ahead) max|delta| = {max_abs_delta!r} "
          f"({'PASS' if max_abs_delta == 0.0 else 'FAIL'})")

    for cost_bps in args.cost_bps:
        cfg = base_cfg.with_(cost_bps_per_side=cost_bps)
        run_scenario(close, cfg, label=f"cost{cost_bps:g}bps", daily_variant=args.daily_variant)

    run_benchmark(close)


if __name__ == "__main__":
    main()
