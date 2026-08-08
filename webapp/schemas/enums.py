"""Code-level enums for fields stored as plain strings in webapp/models.py.

DB columns (Account.broker/env/strategy) are plain String, not a DB enum type
— a new value (new broker, new env state) never needs a migration. These
enums exist for self-documentation/type hints; the actual value space is
enforced by webapp/schemas/accounts.py's Pydantic validation at the write
boundary, not by the DB.
"""
from __future__ import annotations

from enum import Enum


class Broker(str, Enum):
    CTRADER = "CTRADER"
    BYBIT = "BYBIT"


class Env(str, Enum):
    DEMO = "demo"
    LIVE = "live"
    TESTNET = "testnet"
    MAINNET = "mainnet"


class Strategy(str, Enum):
    S007 = "S007"
    S009 = "S009"


class LogLevel(str, Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class LogKind(str, Enum):
    """Curated business events only (see webapp/schemas/logs.py) — the full
    per-tick/debug volume goes to stdout, not this table (see the
    multi-account-architecture memory's "Logging split" note). Extend this
    as new curated event kinds are added; it is not meant to mirror every
    kind utils/trade_logger.StrategyLogger's file logs already carry."""
    CYCLE_START = "cycle_start"
    CYCLE_END = "cycle_end"
    POSITION_OPEN = "position_open"
    POSITION_CLOSE = "position_close"
    SKIP_REOPEN = "skip_reopen"
    SKIP_RISK_CAP = "skip_risk_cap"
    LOOP_SETTLED = "loop_settled"
    LOOP_RESUMED = "loop_resumed"
    # written by webapp/sync_positions.py, never by the trading runner
    SYNC = "sync"                        # one sync pass finished (summary counters)
    POSITION_ADOPTED = "position_adopted"  # broker had a position this bot never opened
    ERROR = "error"
