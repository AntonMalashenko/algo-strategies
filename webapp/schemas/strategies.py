"""Strategy domain schema -- one row per strategy (S007, S009, ...), looked
up by name when linking an account to it (see account_strategies.py)."""
from __future__ import annotations

from pydantic import BaseModel

from webapp.schemas.enums import Broker, Strategy


class StrategyCreate(BaseModel):
    name: Strategy
    broker: Broker             # which broker this strategy trades on
    description: str | None = None
    default_preset: str | None = None
