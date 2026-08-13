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
# positive, maxDD -40R) / FILTERED_S007 / WORKING_S007 / WORKING_S007_V2 /
# WORKING_S007_LIQFLOOR (see strategy_spec.md §9-10 / strategy-passport-S007.md).
#
# WORKING_S007 = BASELINE_S007 + day-height filter + B-reversal->A. Was the
# champion for raw profit (net +0.571R/day real-spread, +38% vs base, same
# maxDD -40R, best worst-year +0.29R) — validated 2026-07-16, see
# strategy-passport-S007.md §4c. Chosen for the first live demo run (decision
# 2026-07-19, decisions-log.md): not on a hard-daily-limit prop account, so the
# reversal's better average outweighs its slightly worse daily tail.
#
# PROMOTED 2026-08-12 -> WORKING_S007_LIQFLOOR (ALGODEV-21 fix): WORKING_S007's
# tp_mode="liquidity" could pick a take-profit closer than the standard 100%-
# range target, giving near-zero-R trades that resolve in 1-2 minutes -- too
# fast for this bot's 1-minute poll to reliably catch (real example 2026-08-11,
# see decisions-log.md). WORKING_S007_LIQFLOOR floors the liquidity-TP search at
# range_tp, fixing that. Gate 1/Gate 2 backtest (backtest-log.md 2026-08-11/12,
# both point-cost and bps-cost models) was MIXED, not a clean win: aggregate net
# R/day is higher (+0.80->+0.85R bps vs +0.63R for WORKING_S007), but win-rate is
# lower (60% vs 69%), maxDD is deeper (-47..-51R vs -39R), and 2023 is a much
# weaker year under the fix even after removing the cost-model artifact (+18R vs
# +43R for WORKING_S007) -- see backtest-log.md 2026-08-12 for the full per-year
# table and the root-cause analysis (more pyramided adds accumulate before the
# farther TP is reached, so a subsequent reversal hits more open positions at
# once). Anton made an explicit, informed decision to promote anyway, accepting
# that tradeoff (chat 2026-08-12) -- NOT an automatic backtest-passed promotion,
# flag this context if revisiting the choice.
PRESET = "WORKING_S007_LIQFLOOR"

# --- sizing ---
RISK_PCT = 0.25            # % of CURRENT balance risked per position, refetched every
# order (see bot/risk.py:lots_for_risk). With flat R sizing the max daily loss =
# max_positions x RISK_PCT (=1.0% at 4x0.25). Live 2026-07-21: switched on after the
# first successful live-demo day confirmed real broker connectivity (decisions-log.md).
#
# money_per_point_per_lot is NOT a hand-picked number -- it's read from the broker's
# own ProtoOASymbol.lotSize at the start of every cycle (CTraderS007.run_live_cycle),
# so a leverage/contract-spec change on the broker side can't silently desync it from
# a stale hardcoded constant (decision 2026-07-21, decisions-log.md). CORRECTION
# 2026-07-23: that lotSize-derived value is correct only in the SYMBOL's own quote
# currency -- it does NOT by itself account for a quote-currency/deposit-currency
# mismatch. See EUR_TO_USD_FX_RATE_APPROX below: that mismatch is exactly what was
# missing here, discovered by reconciling real closed trades against the broker's
# own statement (decisions-log.md 2026-07-23).
#
# USE_FIXED_LOT=True is the manual override / emergency fallback (e.g. if the
# broker-fetched money_per_point_per_lot ever looks wrong on a live cycle) —
# flip it back on to trade FIXED_LOT flat, no risk math, no extra broker calls.
USE_FIXED_LOT = False
FIXED_LOT = 0.01          # used directly when USE_FIXED_LOT=True; always the
# broker-minimum-lot floor for risk-based sizing ("0.25%, or 0.01 if it doesn't fit").

# --- daily aggregate risk cap ---
# RISK_PCT above sizes each position independently -- it does NOT look at what's
# already open, so min-lot flooring (see decisions-log.md 2026-07-24) can push the
# REAL summed risk across today's open positions well past max_positions x RISK_PCT.
# DAILY_RISK_CAP_PCT caps the sum of *actual* potential loss (broker's own
# |price-stopLoss| x volume per open S007 position, not our nominal risk_amount) --
# a new entry/add is skipped once open_risk + its own potential loss would exceed
# this fraction of current balance. Chosen 2026-07-24 = 2%.
DAILY_RISK_CAP_PCT = 2.0

# --- currency conversion (GER40/DE40 quotes in EUR; this cTrader account is in USD) ---
# money_per_point_per_lot above is derived from the broker's ProtoOASymbol.lotSize,
# which is correct in the INSTRUMENT's own quote currency (EUR for GER40/DE40) but
# is never itself converted into the account's deposit currency (USD, confirmed
# 2026-07-23 from the account statement's own "Счет : USD" header) -- the 2026-07-21
# assumption "symbol quotes directly in the deposit currency, no FX conversion
# needed" was never verified live and turned out to be wrong. bot/s007_paper.py's
# decide() multiplies the broker-fetched money_per_point_per_lot by this rate before
# using it for any risk_amount/lot-size math, so every $ risk figure is finally in
# real USD terms, not silently ~14% short.
#
# EUR_TO_USD_FX_RATE_APPROX is a MANUALLY MAINTAINED SNAPSHOT, not a live broker
# quote. The fully correct approach is to fetch it live every cycle via
# ProtoOASymbolsForConversionReq (quote asset -> ProtoOATrader.depositAssetId) plus
# a ProtoOASpotEvent subscription for the returned conversion-symbol chain (see
# https://help.ctrader.com/open-api/symbol-rate-conversion/ and decisions-log.md
# 2026-07-23 for the exact mechanism) -- deferred as a future upgrade; not needed
# while EUR/USD stays reasonably close to this snapshot.
#
# Value derived 2026-07-23 from two independent sources that agreed closely:
#   - empirical: mean of (realized P&L / price-move-in-points) across the 8 closed
#     DE40 trades in the broker's own statement, 20-23 Jul 2026 (cT_10085917_...csv)
#     = 1.14268, range 1.1421-1.1448 across 4 days -- tight enough to trust.
#   - live spot EUR/USD quoted that same day (xe.com) = 1.1408.
# Picked the empirical value (very slightly more conservative than the live spot --
# i.e. higher, which computes a slightly SMALLER lot for the same target $ risk, not
# a larger one). EUR/USD moves over time, so THIS NEEDS PERIODIC MANUAL REFRESH:
# re-run the statement-reconciliation check above (or at minimum glance at a live
# EUR/USD quote) every few weeks, or as soon as a live trade's realized P&L stops
# matching money_per_point_per_lot x price-move again.
EUR_TO_USD_FX_RATE_APPROX = 1.1427

# --- session (EET / Kyiv clock, anchored to the DAX cash open at 10:00) ---
FR_START, FR_END = "09:00", "09:59"   # pre-open hour range (see spec §0)
TRADE_START, EXIT_END = "10:00", "16:59"

# --- runtime ---
HISTORY_DAYS = 4           # M1 lookback (need prior day for liquidity levels)
DRY_RUN_DATA = "data/raw/GER40/GER40m1.csv.gz"   # local M1 for --dry-run
PAPER_LOG_DIR = "reports/paper_s007"
MAGIC = "S007"             # label prefix for broker orders
