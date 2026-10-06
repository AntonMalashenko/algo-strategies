"""S004-intraday (ALGODEV-62 phase A): portfolio simulator + base regression.

S004-intraday is the frozen S004 champion plus three prop rules fixed before
looking at results -- see strategies/s004_config.py for the rules and why each
exists. Two of them are engine flags; the third (at most
`max_trades_per_day` entries per server day across the WHOLE 7-pair portfolio)
only makes sense once all pairs are on one account, which is what this module
simulates.

Unlike backtest/run_prop_portfolio.py (the 2026-10-06 research script, which
post-processed the already-finished champion trade list), the cutoff and the
sizing rule here run INSIDE the engine. That matters: a trade closed at 22:45
frees S004's one-position-per-pair lock the same evening, so entries the
post-processed version could never see are taken here. The research run is
therefore a conservative lower bound on this one, not a target to reproduce.

Commands:
    python -m backtest.run_s004_intraday regression  # base unchanged, trade for trade
    python -m backtest.run_s004_intraday run         # S004-intraday metrics + gates
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

from backtest.s004_metalabel_data import load_combined
from strategies.fvg_mtf import run_backtest
from strategies.s004_config import S004Config, S004_BASE, S004_INTRADAY

ROOT = Path(__file__).resolve().parent.parent
REPORTS = ROOT / "reports"
BASE_REFERENCE = REPORTS / "s004_metalabel_dataset.csv"   # frozen champion trade list
INTRADAY_TRADES = REPORTS / "s004_intraday_portfolio_trades.csv"

TRUE_OOS_FROM = pd.Timestamp("2022-03-01")   # S004 E10 untouched window start

# Acceptance gates for phase A, fixed in ALGODEV-62 before the run.
GATE_OOS_MIN_R_PER_TRADE = 0.08
GATE_WORST_DAY_MIN_R = -2.0

# Columns that must match the reference trade for trade. `r` is included:
# an identical R proves the cost model and the exit prices are untouched too.
REGRESSION_COLS = ["symbol", "time_in", "dir", "entry", "sl", "tp", "r", "exit_reason"]
REGRESSION_TOL = 1e-12


def apply_daily_cap(trades: pd.DataFrame, max_trades_per_day: int | None) -> pd.DataFrame:
    """Mark the first `max_trades_per_day` entries of each server day as taken.

    Portfolio-wide (prop rule 3): the cap counts entries across ALL pairs
    together, because all seven run on one account. `trades` must already be
    sorted by entry time. None means no cap.
    """
    capped = trades.copy()
    capped["day"] = capped["time_in"].dt.normalize()
    if max_trades_per_day is None:
        capped["taken"] = True
    else:
        nth_in_day = capped.groupby("day").cumcount() + 1
        capped["taken"] = nth_in_day <= max_trades_per_day
    return capped


def portfolio_trades(cfg: S004Config) -> pd.DataFrame:
    """Run every pair, keep entry-hour trades, apply the portfolio-wide daily cap."""
    per_pair = []
    for symbol in cfg.pairs:
        m15 = load_combined(symbol)
        trades = run_backtest(m15, **cfg.engine_kwargs())
        trades = trades[trades["hour"].isin(cfg.entry_hours)].copy()
        trades["symbol"] = symbol
        per_pair.append(trades)
    all_trades = pd.concat(per_pair, ignore_index=True)
    all_trades = all_trades.sort_values(["time_in", "symbol"]).reset_index(drop=True)
    return apply_daily_cap(all_trades, cfg.max_trades_per_day)


def describe(trades: pd.DataFrame, label: str) -> dict:
    daily_r = trades.groupby("day")["r"].sum()
    equity = daily_r.cumsum()
    yearly_r = trades.groupby(trades["day"].dt.year)["r"].sum()
    stats = dict(
        n=len(trades),
        r_per_trade=trades["r"].mean(),
        total_r=trades["r"].sum(),
        max_dd_r=(equity - equity.cummax()).min(),
        years_positive=int((yearly_r > 0).sum()),
        years=len(yearly_r),
        worst_day_r=daily_r.min(),
    )
    print(f"{label:46s} n={stats['n']:5d} R/trade={stats['r_per_trade']:+.4f} "
          f"total={stats['total_r']:+7.1f} maxDD={stats['max_dd_r']:+6.1f} "
          f"years+={stats['years_positive']}/{stats['years']} "
          f"worst day={stats['worst_day_r']:+.2f}")
    return stats


def regression() -> None:
    """S004_BASE must reproduce the frozen champion trade list exactly."""
    reference = pd.read_csv(BASE_REFERENCE, parse_dates=["time_in"])
    produced = portfolio_trades(S004_BASE)
    assert len(produced) == len(reference), \
        f"trade count {len(produced)} != reference {len(reference)}"
    ref = reference.sort_values(["time_in", "symbol"]).reset_index(drop=True)
    got = produced.sort_values(["time_in", "symbol"]).reset_index(drop=True)
    worst = 0.0
    for col in REGRESSION_COLS:
        if pd.api.types.is_numeric_dtype(ref[col]):
            delta = (got[col].astype(float) - ref[col].astype(float)).abs().max()
            worst = max(worst, float(delta))
            assert delta <= REGRESSION_TOL, f"column '{col}': max|delta| = {delta}"
        else:
            assert got[col].equals(ref[col]), f"column '{col}' differs"
    print(f"regression OK -- {len(got)} trades reproduce {BASE_REFERENCE.name} "
          f"trade for trade, max|delta| = {worst:g}")


def run() -> None:
    trades = portfolio_trades(S004_INTRADAY)
    trades.to_csv(INTRADAY_TRADES, index=False)
    print(f"exit reasons: {trades.exit_reason.value_counts().to_dict()}")
    describe(trades, "all signals (before the daily cap)")
    taken = trades[trades["taken"]]
    describe(taken, "S004-intraday (max 2/day, whole portfolio)")
    oos = taken[taken["time_in"] >= TRUE_OOS_FROM]
    describe(taken[taken["time_in"] < TRUE_OOS_FROM], "  pre-2022-03 (research window)")
    oos_stats = describe(oos, "  2022-03+ (untouched window)")
    for symbol, pair_trades in oos.groupby("symbol"):
        print(f"   OOS {symbol}: n={len(pair_trades)} R/trade={pair_trades['r'].mean():+.3f}")

    print(f"\nwrote {INTRADAY_TRADES}")
    gates = {
        f"OOS R/trade >= {GATE_OOS_MIN_R_PER_TRADE}":
            oos_stats["r_per_trade"] >= GATE_OOS_MIN_R_PER_TRADE,
        "OOS every year positive":
            oos_stats["years_positive"] == oos_stats["years"],
        f"worst day >= {GATE_WORST_DAY_MIN_R}R":
            oos_stats["worst_day_r"] >= GATE_WORST_DAY_MIN_R,
    }
    for name, passed in gates.items():
        print(f"gate {'PASS' if passed else 'FAIL'}: {name}")
    if not all(gates.values()):
        sys.exit(1)


if __name__ == "__main__":
    command = sys.argv[1] if len(sys.argv) > 1 else "run"
    if command == "regression":
        regression()
    elif command == "run":
        run()
    else:
        raise SystemExit(f"unknown command {command!r} (regression|run)")
