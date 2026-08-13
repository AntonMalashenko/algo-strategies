"""Domain objects and validation schemas for webapp/ — also the home for
future API request/response schemas (same models, reused, not re-modeled).

Submodules:
  enums.py             — code-level enums (Broker, Env, Strategy, BrokerMode,
                          LogLevel, LogKind) for the plain-string columns in
                          webapp/models.py (DB columns stay strings so a new
                          enum value never needs a migration; these enums +
                          the Pydantic models below are where that value
                          space is actually enforced).
  accounts.py           — AccountCreate (broker identity + credentials) +
                          broker-specific credential shapes.
  strategies.py         — StrategyCreate (one row per strategy, e.g. S007).
  account_strategies.py — AccountStrategyCreate: links an Account to a
                          Strategy plus that pair's own enabled/config/status
                          (an account can run several strategies at once).
  logs.py               — LogEntryCreate: curated business events only (see
                          the multi-account-architecture memory's "Logging
                          split" note — full debug volume goes to stdout).
  users.py              — UserCreate.
"""
from __future__ import annotations

from webapp.schemas.account_strategies import AccountStrategyCreate
from webapp.schemas.accounts import (
    AccountCreate, BybitCredentials, CtraderCredentials,
    CREDENTIALS_BY_BROKER, ENV_BY_BROKER,
)
from webapp.schemas.enums import BrokerMode, Broker, Env, LogKind, LogLevel, Strategy
from webapp.schemas.logs import LogEntryCreate
from webapp.schemas.strategies import StrategyCreate
from webapp.schemas.users import UserCreate

__all__ = [
    "AccountCreate", "BybitCredentials", "CtraderCredentials",
    "CREDENTIALS_BY_BROKER", "ENV_BY_BROKER",
    "AccountStrategyCreate", "StrategyCreate", "LogEntryCreate",
    "Broker", "Env", "Strategy", "BrokerMode", "LogLevel", "LogKind",
    "UserCreate",
]
