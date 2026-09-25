"""S021 (ORB -- opening range breakout, Nasdaq 100) live/paper configuration.

Frozen strategy math (k_range, stop_adr_mult, session times, ADR window) is
NOT duplicated here -- it lives in strategies/orb_intraday/config.py::ORB_BASE,
the same OrbConfig the independently-verified backtest engine
(strategies/orb_intraday/engine.py) uses, so live and backtest can never
drift apart into two competing copies of the rules (see claude/conventions.md
and strategy-passport-S021.md sec 10: "не переоптимизировать k... новые
варианты только отдельными пресетами"). This module only holds RUNTIME
concerns: which broker symbol to trade, position sizing, and bot bookkeeping
-- things that have no backtest equivalent to drift from.

Broker credentials are the SAME as S007/S004 -- read from <repo>/.env:

    CTRADER_CLIENT_ID / CTRADER_CLIENT_SECRET / CTRADER_ACCESS_TOKEN
    CTRADER_ACCOUNT_ID   (numeric ctidTraderAccountId of the DEMO account)
    CTRADER_HOST         (optional, defaults to demo.ctraderapi.com)
"""
from __future__ import annotations

from strategies.orb_intraday.config import ORB_BASE, OrbConfig

# --- strategy math: single source of truth ---
# Do not override fields here for a "live variant" -- see strategies/
# orb_intraday/config.py's own docstring: a variant is a new preset via
# .with_(...), not a live-only copy that can silently diverge from the
# backtested rules. STRATEGY is what bot/orb_signals.py computes
# ADR14/O/U/L from -- exactly the same object simulate() in engine.py uses.
STRATEGY: OrbConfig = ORB_BASE

# --- instrument ---
# cTrader symbol name for Nasdaq 100; brokers differ (US100 / NAS100 / USTEC
# / NAS100.cash / NDX100). The bot resolves the first match from this list
# against the account's live symbol list (mirrors bot/s007_config.py's
# SYMBOL_CANDIDATES -- see bot/symbol_resolver.py for how a verified
# webapp.models.BrokerAssetSymbol row takes priority over this fallback).
# NOT yet verified against any real broker's actual symbol list (S021 has no
# live/demo deployment yet) -- verify on first --check and run
# `webapp.cli verify-symbol` once confirmed, same as S007's ALGODEV-31.
SYMBOL_CANDIDATES = ["US100", "NAS100", "USTEC", "NAS100.cash", "US100.cash", "NDX100", "NSXUSD"]

# webapp.models.Asset.symbol / webapp.models.BrokerAssetSymbol.platform this
# strategy trades -- the (broker_id, ASSET_SYMBOL, PLATFORM) key
# bot/symbol_resolver.py::resolve_symbol() looks up. No Asset row is assumed
# to exist yet -- resolve_symbol() falls back to SYMBOL_CANDIDATES (logged
# at WARNING) when it doesn't, so this is safe before any DB seeding.
ASSET_SYMBOL = "NAS100"
PLATFORM = "CTRADER"

# --- sizing ---
# Gate 3 (prod-reality prop simulation, strategy-passport-S021.md sec 4):
# best cashout/bust tradeoff over the risk grid was 0.50-0.75%/trade -- this
# picks the middle of that range as the default; override per-account via
# AccountStrategy.risk_pct (webapp/models.py), same mechanism as S007.
RISK_PCT = 0.5

# Unlike S007/GER40 (EUR-quoted against a USD account -- see
# bot.s007_config.EUR_TO_USD_FX_RATE_APPROX), every Nasdaq 100 CFD candidate
# above is a USD-quoted index product on the brokers checked so far, so no
# quote-to-deposit-currency conversion is applied here. NOT independently
# verified against a live broker statement the way S007's rate was
# (decisions-log.md 2026-07-23) -- if money_per_point_per_lot x price-move
# ever stops matching realized P&L on the account statement, that assumption
# is the first thing to re-check (same class of bug as S007's FX gap, just
# not yet hit here because S021 has no live history at all).
USE_FIXED_LOT = False
FIXED_LOT = 0.01          # broker-minimum-lot floor, same convention as S007

# --- runtime ---
# Calendar-day window of the M15 trendbar fetch that ADR14's HISTORY is
# reconstructed from (bot/ctrader_orb.py::_get_m15_step). Until 2026-09-22
# this was an M1 window, but cTrader caps a trendbars response at roughly
# 14000 bars whatever window is requested -- on M1 that is only ~13 session
# days, below the 14 ADR14 needs, so ADR14 was always NaN live. ADR14 needs
# 14 valid prior sessions; 30 calendar days leaves slack for weekends/
# holidays/data gaps so the fetch reliably covers >= 14 valid trading days
# even after a gap (see strategy-passport-S021.md sec 7.1 on the known
# histdata gaps -- live cTrader data shouldn't have those, but the margin is
# cheap). At M15, 30 days is under ~3000 bars even for a near-24h CFD
# (<= 96 bars/day), comfortably under the cap: each M15 bar covers 15x the
# time an M1 bar does, so the same cap now reaches ~15x further back --
# roughly an order of magnitude more history than ADR14 needs, where M1
# fell just short of it.
HISTORY_DAYS = 30

# Calendar-day window of the M1 trendbar fetch (bot/ctrader_orb.py::
# _get_m1_step) -- it only has to contain "today": the 09:30 anchor bar and
# a precise "now" reading for decide()'s 14:29/15:59 checks. Deliberately
# small, since it no longer covers ADR history (that is HISTORY_DAYS, on
# M15). 3 days is well beyond the single day actually needed (the margin is
# cheap) and still at most a few thousand M1 bars, far under the bar cap.
TODAY_M1_DAYS = 3

# LIVE_EXIT_BUFFER_MIN (found live 2026-09-24: a position opened 2026-09-23
# was still open the next morning -- the 23:59:04 Kyiv cycle, the LAST one
# inside deployment/schedule.yml's cron window that day, only saw the M1 bar
# at 15:57 fixed-EST, 2 minutes short of the 15:59 time-exit in
# bot/orb_signals.py's decide(); the window then closed for the day with
# nothing left to catch it. Worse: the next day's cycles compute today's
# labels fresh (_today_label keyed off the CURRENT session's anchor date),
# so the stale position's yesterday-dated label is never matched again --
# decide() can no longer see it at all, let alone close it).
#
# Fix: subtract this many minutes from strategies.orb_intraday.config
# ::ORB_BASE.session_close ONLY for the live time-exit check
# (bot/orb_signals.py's `now_t >= ...` comparison) -- deliberately NOT by
# changing session_close itself, which is also read by the frozen backtest
# engine (strategies/orb_intraday/engine.py's session-range/level math) and
# by decide()'s own ADR-session-validity threshold
# (session_close - session_open -> session_minutes) -- neither of those
# should move, this is a live-only operational safety margin, not a
# strategy-edge change (strategy-passport-S021.md sec 10: "не
# переоптимизировать"). At the live-effective 15:49, the cron window (ends
# 20:59 UTC = 15:59 fixed-EST) leaves 10 one-minute cycles of slack to catch
# and execute the close instead of 0.
LIVE_EXIT_BUFFER_MIN = 10

MAGIC = "S021"             # label prefix for broker orders/positions; matches
                           # the Strategy.name row this must be registered under
