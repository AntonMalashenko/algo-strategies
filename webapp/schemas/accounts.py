"""Account domain schema + broker-specific credential shapes.

AccountCreate is the validated shape every Account write goes through —
built from configs/accounts.yml, a future API payload, or CLI args, then
handed to the ORM (webapp/models.py) via `.model_dump()` with `credentials`
JSON-encoded. The ORM/DB layer does not re-validate broker/env/credential
shape; this is the one place that does.
"""
from __future__ import annotations

from pydantic import BaseModel, model_validator

from webapp.schemas.enums import Broker, Env

# Which Env values are valid for which broker -- a bool (is_live) can't
# express Bybit's 3-way demo/testnet/mainnet split, so env is a string
# validated per-broker here instead.
ENV_BY_BROKER: dict[Broker, set[Env]] = {
    Broker.CTRADER: {Env.DEMO, Env.LIVE},
    Broker.BYBIT: {Env.DEMO, Env.TESTNET, Env.MAINNET},
}


class CtraderCredentials(BaseModel):
    client_id: str
    client_secret: str
    access_token: str


class BybitCredentials(BaseModel):
    api_key: str
    api_secret: str


CREDENTIALS_BY_BROKER: dict[Broker, type[BaseModel]] = {
    Broker.CTRADER: CtraderCredentials,
    Broker.BYBIT: BybitCredentials,
}


class AccountCreate(BaseModel):
    """The account itself -- broker identity + credentials. Which
    strategy(-ies) it runs, and each one's config/status, is a separate
    concern -- see webapp/schemas/account_strategies.py's AccountStrategyCreate,
    since one account can run several strategies at once."""
    user_id: int
    broker: Broker
    external_account_id: str | None = None   # cTrader ctidTraderAccountId; unused for Bybit
    env: Env
    label: str = ""
    broker_host: str | None = None

    credentials: dict

    @model_validator(mode="after")
    def _check_broker_specific(self) -> "AccountCreate":
        allowed_envs = ENV_BY_BROKER[self.broker]
        if self.env not in allowed_envs:
            raise ValueError(
                f"env={self.env.value!r} not valid for broker={self.broker.value!r}; "
                f"allowed: {sorted(e.value for e in allowed_envs)}")
        cred_cls = CREDENTIALS_BY_BROKER[self.broker]
        cred_cls(**self.credentials)   # raises pydantic.ValidationError on bad/missing fields
        return self
