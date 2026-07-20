"""S007 (GER40 London x Frankfurt) live/paper configuration.

Frozen to the strategy spec (docs/strategy_spec.md / project passport S007).
Broker credentials are the SAME as S004 — read from <repo>/.env:

    CTRADER_CLIENT_ID / CTRADER_CLIENT_SECRET / CTRADER_ACCESS_TOKEN
    CTRADER_ACCOUNT_ID   (numeric ctidTraderAccountId of the DEMO account)
    CTRADER_HOST         (optional, defaults to demo.ctraderapi.com)
"""
from __future__ import annotations

import os
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except ImportError:
    pass


def cred(name, default=None):
    return os.environ.get(name, default)


# --- instrument ---
# cTrader symbol name for the German index; brokers differ (GER40 / DE40 /
# Germany 40 / GER40.cash). The bot resolves the first match from this list.
SYMBOL_CANDIDATES = ["GER40", "DE40", "GERMANY40", "GER40.cash", "DE40.cash", "GER30"]

# --- strategy preset (name from ger40_lonfra.config) ---
# Options: BASELINE_S007 (frozen base, net +0.415R/day real-spread, all years
# positive, maxDD -40R) / FILTERED_S007 / WORKING_S007 / WORKING_S007_V2
# (see strategy_spec.md §9-10 / strategy-passport-S007.md).
#
# WORKING_S007 = BASELINE_S007 + day-height filter + B-reversal->A. Recommended
# champion for raw profit (net +0.571R/day real-spread, +38% vs base, same
# maxDD -40R, best worst-year +0.29R) — validated 2026-07-16, see
# strategy-passport-S007.md §4c. Chosen for the first live demo run (decision
# 2026-07-19, decisions-log.md): not on a hard-daily-limit prop account, so the
# reversal's better average outweighs its slightly worse daily tail.
PRESET = "WORKING_S007"

# --- sizing ---
RISK_PCT = 0.25            # % of balance risked per 1R (position). 0.25 -> ~-10% maxDD
# With flat R sizing the max daily loss = max_positions x RISK_PCT (=1.0% at 4x0.25).
# For the FIRST demo run use a fixed small lot (risk-% sizing needs the broker's
# contract/tick value — calibrate once, then switch USE_FIXED_LOT=False).
USE_FIXED_LOT = True
FIXED_LOT = 0.01

# --- session (EET / Kyiv clock, anchored to the DAX cash open at 10:00) ---
FR_START, FR_END = "09:00", "09:59"   # pre-open hour range (see spec §0)
TRADE_START, EXIT_END = "10:00", "16:59"

# --- runtime ---
HISTORY_DAYS = 4           # M1 lookback (need prior day for liquidity levels)
DRY_RUN_DATA = "data/raw/GER40/GER40m1.csv.gz"   # local M1 for --dry-run
PAPER_LOG_DIR = "reports/paper_s007"
MAGIC = "S007"             # label prefix for broker orders
