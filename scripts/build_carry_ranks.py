"""Build a monthly G10 carry-rank table from the rate files in data/raw/rates/.

S005 (FX carry) step E1: turn per-currency short-term rates into a single
wide table with one row per month and, for each currency, its rate and its
rank that month (1 = highest rate = most attractive to fund a long carry
leg; 10 = lowest = funding leg for a short). This table is the direct input
for the E2 backtest (long top-3 / short bottom-3, equal-weight, monthly
rebalance).

Run after scripts/fetch_rates.py, on a machine with the data/raw/rates/*.csv
files present (works fully offline once those exist -- no network needed
here):

    python scripts/build_carry_ranks.py

Output: data/processed/carry_ranks.csv, columns:
    date, <CCY>_rate for each currency, <CCY>_rank for each currency
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
RATES_DIR = ROOT / "data" / "raw" / "rates"
OUT = ROOT / "data" / "processed" / "carry_ranks.csv"


def load_rates() -> pd.DataFrame:
    """Load every data/raw/rates/<CCY>.csv into one wide, month-end-indexed frame."""
    series = {}
    for path in sorted(RATES_DIR.glob("*.csv")):
        ccy = path.stem
        df = pd.read_csv(path, parse_dates=["date"])
        df = df.set_index("date")["rate"]
        df.index = df.index.to_period("M").to_timestamp("M")
        series[ccy] = df
    if not series:
        raise FileNotFoundError(
            f"No rate files found in {RATES_DIR} -- run scripts/fetch_rates.py first."
        )
    wide = pd.DataFrame(series).sort_index()
    # A handful of vintages start later than others (e.g. EUR only from 1999);
    # forward-fill small individual gaps but never fabricate a currency's
    # entire pre-history -- rows missing every value stay missing.
    wide = wide.ffill(limit=2)
    return wide


def add_ranks(wide: pd.DataFrame) -> pd.DataFrame:
    """Append a <CCY>_rank column per currency: 1 = highest rate that month."""
    ranks = wide.rank(axis=1, ascending=False, method="average")
    out = wide.add_suffix("_rate").join(ranks.add_suffix("_rank"))
    return out.sort_index(axis=1)


def main():
    wide = load_rates()
    table = add_ranks(wide)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    table.to_csv(OUT)
    n_complete = wide.dropna().shape[0]
    print(f"Currencies: {list(wide.columns)}")
    print(f"Months: {len(wide)}  ({wide.index.min().date()}..{wide.index.max().date()})")
    print(f"Months with all currencies present: {n_complete}")
    print(f"-> {OUT}")


if __name__ == "__main__":
    main()
