"""Monthly top-up of all FX/metals and index M15 data already tracked in data/raw/.

Re-runs the same histdata.com pipeline used to build the initial
data/raw/<SYM>/<SYM>m15fresh.csv files (scripts/fetch_histdata.py +
scripts/convert_histdata.py / convert_histdata_indices.py), but only for the
pairs already present in the repo, and only re-downloads the still-open
current month (past months/years are cached in data/histdata/ and skipped).
Safe to run repeatedly -- each run just tops up what's changed since last time.

Usage:
    python scripts/refresh_monthly.py

Schedule monthly with cron, e.g. (3am on the 1st of each month):
    0 3 1 * * cd /path/to/algo && .venv/bin/python scripts/refresh_monthly.py >> logs/refresh_monthly.log 2>&1
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import convert_histdata
import convert_histdata_indices
import fetch_histdata

# FX/metals pairs: histdata.com symbol == repo folder name (lowercased).
# Matches the pairs already fetched into data/raw/<SYM>/<SYM>m15fresh.csv.
FX_PAIRS = [
    "eurusd", "gbpusd", "usdjpy", "usdchf", "audusd", "eurjpy", "gbpjpy",
    "audjpy", "eurchf", "eurgbp", "usdcad", "xauusd",
]
FX_START_YEAR = 2022

# Index CFDs: histdata.com symbol -> repo folder name, see
# convert_histdata_indices.INDEX_MAP. No histdata.com equivalent for
# US2000M (Russell 2000), so it isn't refreshed here.
INDEX_PAIRS = ["grxeur", "frxeur", "etxeur", "ukxgbp", "nsxusd", "spxusd"]
INDEX_START_YEAR = 2019


def main():
    print("=== Fetching FX/metals (current month top-up) ===")
    fetch_histdata.fetch_pairs(FX_PAIRS, FX_START_YEAR, refetch_current_month=True)

    print("\n=== Fetching indices (current month top-up) ===")
    fetch_histdata.fetch_pairs(INDEX_PAIRS, INDEX_START_YEAR, refetch_current_month=True)

    print("\n=== Rebuilding FX/metals M15 fresh files ===")
    convert_histdata.main()

    print("\n=== Rebuilding index M15 fresh files ===")
    convert_histdata_indices.main()

    print("\nMonthly refresh complete.")


if __name__ == "__main__":
    main()
