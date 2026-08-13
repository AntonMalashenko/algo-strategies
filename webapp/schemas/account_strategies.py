"""AccountStrategy domain schema -- links one Account to one Strategy plus
that pair's own config/status (an account may run several strategies at
once, each independently enabled/configured).

Note: this schema only validates shape (field types/ranges). Checking that
the linked Strategy.broker actually matches the linked Account.broker needs
both rows loaded from the DB, so that check belongs in the service/CRUD layer
that has a session, not here -- Pydantic models in this package stay DB-free.
"""
from __future__ import annotations

from pydantic import BaseModel, Field

from webapp.schemas.enums import BrokerMode


class AccountStrategyCreate(BaseModel):
    account_id: int
    strategy_id: int
    enabled: bool = False
    preset: str | None = None
    symbol: str | None = None
    risk_pct: float = Field(default=0.25, ge=0)
    fixed_lot: float = Field(default=0.01, gt=0)
    use_fixed_lot: bool = True
    initial_balance: float | None = Field(default=None, ge=0)
    broker_mode: BrokerMode = BrokerMode.OFF
