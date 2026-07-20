"""Runner for the S005 FX carry backtest (E2 baseline + E3 realistic costs).

Pipeline: rate data + spot data -> monthly currency returns -> carry
portfolio (strategies/fx_carry.py) -> equity -> metrics and report.

Usage:
    python -m backtest.run_carry
    python -m backtest.run_carry --spread-bps 4 --swap-haircut 0.3

Data expected on disk (see scripts/fetch_rates.py and
scripts/fetch_g10_spot.py for how to produce it):
    data/raw/rates/<CCY>.csv        columns: date, rate   (10 files, incl. USD)
    data/raw/spot_g10/<PAIR>.csv    columns: date, close  (9 files)

IS/OOS discipline mirrors S004: the in-sample window (2002-04..2018-12)
covers both mandatory stress periods (2008 GFC, 2015 CHF unpeg) and is the
only window whose metrics this script prints. 2019-01 onward is computed
and saved to reports/, but deliberately not printed here -- it stays
reserved as true out-of-sample until a dedicated final-validation pass
(same discipline as S004's E10), not peeked at while still designing E2/E3.

E3: this runner now builds TWO portfolios over the same IS window -- the
frictionless E2 baseline (spread_bps=0, swap_haircut=0, unchanged) and a
"realistic costs" scenario, printed side by side. Defaults (4 bps round-trip
spread, 30% swap haircut) are a conservative guess, not a measured broker
number -- replace them once S004's paper stage has actual swap data.
"""
from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

from strategies.fx_carry import NON_USD_CURRENCIES, PAIR_MAP, monthly_ccy_returns, build_carry_portfolio
from utils.report import make_report

ROOT = Path(__file__).resolve().parent.parent
RATES_DIR = ROOT / "data" / "raw" / "rates"
SPOT_DIR = ROOT / "data" / "raw" / "spot_g10"
REPORTS_DIR = ROOT / "reports"

PERIODS_PER_YEAR = 12  # monthly rebalance

IS_START = "2002-04-30"
IS_END = "2018-12-31"

STRESS_WINDOWS = {
    "2008 GFC": ("2008-01-01", "2008-12-31"),
    "2015 CHF unpeg": ("2014-11-01", "2015-06-30"),
}


def load_rates() -> pd.DataFrame:
    """Load data/raw/rates/*.csv (incl. USD) into one month-end-indexed frame."""
    series = {}
    for path in sorted(RATES_DIR.glob("*.csv")):
        ccy = path.stem
        df = pd.read_csv(path, parse_dates=["date"])
        s = df.set_index("date")["rate"]
        s.index = s.index.to_period("M").to_timestamp("M")
        series[ccy] = s
    wide = pd.DataFrame(series).sort_index()
    return wide.ffill(limit=2)  # tolerate small individual reporting gaps only


def load_spot() -> dict[str, pd.Series]:
    """Load data/raw/spot_g10/<PAIR>.csv for every pair referenced in PAIR_MAP."""
    out = {}
    for _ccy, (pair, _is_base) in PAIR_MAP.items():
        path = SPOT_DIR / f"{pair}.csv"
        df = pd.read_csv(path, parse_dates=["date"])
        out[pair] = df.set_index("date")["close"]
    return out


def stress_report(equity: pd.Series, monthly_returns: pd.Series, label: str,
                   start: str, end: str) -> None:
    """Print both the aggregate window return AND its month-by-month path.

    A blended multi-month figure can hide a sharp single-month event behind
    a later recovery (this is exactly what happened with the first pass at
    the 2015 CHF-unpeg window: Jan 2015 alone was -3.5%, but Feb's rebound
    made the Jan-Mar aggregate look like +4.5%). Printing the monthly path
    is what actually lets you see the event, not just its aftermath.
    """
    window = equity.loc[start:end]
    if len(window) < 2:
        print(f"  {label}: no data in window")
        return
    total_ret = window.iloc[-1] / window.iloc[0] - 1
    dd = (window / window.cummax() - 1).min()
    print(f"  {label} ({window.index.min().date()}..{window.index.max().date()}): "
          f"aggregate return {total_ret:+.2%}, max drawdown {dd:.2%}")
    for dt, r in monthly_returns.loc[start:end].items():
        print(f"    {dt.date()}: {r:+.2%}")


def run_scenario(rates: pd.DataFrame, ccy_returns: pd.DataFrame, name: str,
                  n_long: int, n_short: int, spread_bps: float, swap_haircut: float,
                  print_stress: bool) -> pd.DataFrame:
    portfolio = build_carry_portfolio(rates, ccy_returns, n_long=n_long, n_short=n_short,
                                       spread_bps=spread_bps, swap_haircut=swap_haircut)
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    portfolio.to_csv(REPORTS_DIR / f"carry_monthly_returns_{name}.csv")

    is_portfolio = portfolio.loc[IS_START:IS_END]
    oos_portfolio = portfolio.loc[pd.Timestamp(IS_END) + pd.Timedelta(days=1):]

    print(f"\n--- {name} | IN-SAMPLE ({IS_START}..{IS_END}): {len(is_portfolio)} months ---")
    is_equity = (1 + is_portfolio["portfolio_return"]).cumprod() * 10_000.0
    make_report(is_equity, is_portfolio["portfolio_return"], name=f"S005_carry_{name}_IS",
                periods_per_year=PERIODS_PER_YEAR, reports_dir=REPORTS_DIR)

    if print_stress:
        print(f"\n--- {name} | Mandatory IS stress windows ---")
        for label, (start, end) in STRESS_WINDOWS.items():
            stress_report(is_equity, is_portfolio["portfolio_return"], label, start, end)

    # Reserved true OOS: computed and saved, but not printed/inspected here.
    if len(oos_portfolio):
        oos_equity = (1 + oos_portfolio["portfolio_return"]).cumprod() * 10_000.0
        oos_equity.to_csv(REPORTS_DIR / f"carry_oos_reserved_equity_{name}.csv")
        print(f"--- {name} | OOS reserved: {len(oos_portfolio)} months from "
              f"{oos_portfolio.index.min().date()} saved, NOT evaluated here ---")

    return portfolio


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n-long", type=int, default=3)
    ap.add_argument("--n-short", type=int, default=3)
    ap.add_argument("--spread-bps", type=float, default=4.0,
                     help="round-trip spread cost in bps of notional, conservative guess")
    ap.add_argument("--swap-haircut", type=float, default=0.3,
                     help="fraction of the rate differential a retail broker's swap markup eats")
    args = ap.parse_args()

    rates = load_rates()
    spot = load_spot()
    ccy_returns = monthly_ccy_returns(spot)

    print("=" * 70)
    print("SCENARIO 1/2: gross / frictionless (E2 baseline, unchanged)")
    print("=" * 70)
    run_scenario(rates, ccy_returns, "gross", args.n_long, args.n_short,
                 spread_bps=0.0, swap_haircut=0.0, print_stress=True)

    print("\n" + "=" * 70)
    print(f"SCENARIO 2/2: realistic costs (spread {args.spread_bps} bps, "
          f"swap haircut {args.swap_haircut:.0%}) -- E3")
    print("=" * 70)
    run_scenario(rates, ccy_returns, "realistic", args.n_long, args.n_short,
                 spread_bps=args.spread_bps, swap_haircut=args.swap_haircut,
                 print_stress=True)


if __name__ == "__main__":
    main()
