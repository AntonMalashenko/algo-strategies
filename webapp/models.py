"""ORM models: users (login only), accounts (1 user -> many, any broker),
strategies (lookup table), account_strategies (association object — an
account may run several strategies, each with its own enabled/config/status),
positions, logs (curated business events for the future API/UI).

Broker credentials live on Account, not User (a user may hold accounts on
several brokers with unrelated credential shapes) — one encrypted JSON blob
per account, shape depends on `broker`. Every write should go through
webapp/schemas/ first; this module does not re-validate broker/env/strategy/
credential shape itself. See the "DB schema conventions" memory for why
broker/env/strategy.name/strategy.broker are plain string columns here
rather than a DB enum type.
"""
from __future__ import annotations

import json
from datetime import datetime

from sqlalchemy import (BigInteger, Boolean, DateTime, Float, ForeignKey, Index,
                        Integer, String, Text, UniqueConstraint)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from webapp.db import Base
from webapp.crypto import encrypt_secret, decrypt_secret


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    accounts: Mapped[list["Account"]] = relationship(
        back_populates="user", cascade="all, delete-orphan")


class Account(Base):
    __tablename__ = "accounts"
    __table_args__ = (UniqueConstraint("user_id", "broker", "external_account_id",
                                       name="uq_user_broker_account"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)

    # broker/env: plain strings by design (see the "DB schema conventions"
    # memory) -- validated against webapp/schemas/enums.py plus broker-
    # specific rules in webapp/schemas/accounts.py before any write, not
    # enforced at the DB level.
    broker: Mapped[str] = mapped_column(String(16), nullable=False)
    external_account_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    env: Mapped[str] = mapped_column(String(16), nullable=False)
    label: Mapped[str] = mapped_column(String(64), default="")
    broker_host: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # broker credentials: one encrypted JSON blob, shape depends on `broker`
    # (see webapp/schemas/accounts.py's CREDENTIALS_BY_BROKER) -- set/read via
    # the `credentials` property, never the raw _enc column.
    _credentials_enc: Mapped[str | None] = mapped_column("credentials_enc", Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    user: Mapped["User"] = relationship(back_populates="accounts")
    # Which strategies this account runs + per-(account,strategy) config/status
    # -- NOT which strategy "the account is" (an account can run several at
    # once, e.g. S007 and a future S00X on the same cTrader account), so this
    # config lives on the association object, not here.
    strategy_links: Mapped[list["AccountStrategy"]] = relationship(
        back_populates="account", cascade="all, delete-orphan")
    positions: Mapped[list["Position"]] = relationship(
        back_populates="account", cascade="all, delete-orphan")

    @property
    def credentials(self) -> dict:
        raw = decrypt_secret(self._credentials_enc)
        return json.loads(raw) if raw else {}

    @credentials.setter
    def credentials(self, value: dict | None):
        self._credentials_enc = encrypt_secret(json.dumps(value) if value else None)


class Strategy(Base):
    """Lookup row per strategy (S007, S009, ...). `broker` records which
    broker the strategy needs (CTRADER/BYBIT/...) -- checked in the service
    layer against the linked Account.broker when creating an AccountStrategy,
    not via a DB constraint (see the "DB schema conventions" memory)."""
    __tablename__ = "strategies"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(16), unique=True, nullable=False)
    broker: Mapped[str] = mapped_column(String(16), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    default_preset: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    account_links: Mapped[list["AccountStrategy"]] = relationship(back_populates="strategy")


class AccountStrategy(Base):
    """Association object (many-to-many Account<->Strategy) carrying the
    per-pair config/status that used to live directly on Account: an account
    running two strategies needs independent enabled/preset/risk/status for
    each, not one shared value."""
    __tablename__ = "account_strategies"
    __table_args__ = (UniqueConstraint("account_id", "strategy_id",
                                       name="uq_account_strategy"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), nullable=False)
    strategy_id: Mapped[int] = mapped_column(ForeignKey("strategies.id"), nullable=False)

    enabled: Mapped[bool] = mapped_column(Boolean, default=False)        # start/stop switch
    preset: Mapped[str | None] = mapped_column(String(32), nullable=True)  # overrides Strategy.default_preset
    symbol: Mapped[str | None] = mapped_column(String(32), nullable=True)  # resolved/forced symbol
    risk_pct: Mapped[float] = mapped_column(Float, default=0.25)
    fixed_lot: Mapped[float] = mapped_column(Float, default=0.01)
    use_fixed_lot: Mapped[bool] = mapped_column(Boolean, default=True)
    # seed capital for this (account, strategy) pair's paper/shadow ledger
    # (e.g. S009's reports/paper_s009/ledger.csv) -- NOT live balance, which
    # is always fetched fresh from the broker each cycle.
    initial_balance: Mapped[float | None] = mapped_column(Float, nullable=True)

    # runtime status (updated by the runner each cycle; UI reads it)
    status: Mapped[str] = mapped_column(String(32), default="idle")
    last_cycle_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    account: Mapped["Account"] = relationship(back_populates="strategy_links")
    strategy: Mapped["Strategy"] = relationship(back_populates="account_links")


class Position(Base):
    """Bot-tracked position lifecycle (mirrors the file log; here for the UI)."""
    __tablename__ = "positions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), nullable=False)
    strategy_id: Mapped[int] = mapped_column(ForeignKey("strategies.id"), nullable=False)
    label: Mapped[str] = mapped_column(String(64), nullable=False)      # engine label (unique/day)
    side: Mapped[str] = mapped_column(String(4))
    entry: Mapped[float] = mapped_column(Float)
    sl: Mapped[float] = mapped_column(Float)
    tp: Mapped[float] = mapped_column(Float)
    is_add: Mapped[bool] = mapped_column(Boolean, default=False)
    # BigInteger: cTrader's positionId is an int64 in the protocol, and a
    # Postgres INTEGER would eventually overflow (SQLite does not care).
    broker_position_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="open")     # open/closed
    reason: Mapped[str | None] = mapped_column(String(32), nullable=True)  # target/flat/...
    opened_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # --- broker truth, written ONLY by webapp/sync_positions.py -------------
    # All nullable: a row the runner just opened has none of this until the
    # first sync, and a broker that cannot report it (Bybit's net-position
    # view has no per-position deal) leaves it null forever. Null therefore
    # means "unknown", never "zero" -- UI aggregates must skip nulls rather
    # than coerce them to 0, or an unsynced position would silently read as
    # a break-even trade.
    origin: Mapped[str] = mapped_column(String(16), default="bot")   # bot | adopted
    exit_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    volume_lots: Mapped[float | None] = mapped_column(Float, nullable=True)
    # money, in the account's deposit currency; cTrader reports the three
    # components separately and `pnl` is their sum (commission and swap come
    # through already signed), so a disputed number can be taken apart again.
    gross_profit: Mapped[float | None] = mapped_column(Float, nullable=True)
    swap: Mapped[float | None] = mapped_column(Float, nullable=True)
    commission: Mapped[float | None] = mapped_column(Float, nullable=True)
    pnl: Mapped[float | None] = mapped_column(Float, nullable=True)
    broker_deal_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    synced_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    account: Mapped["Account"] = relationship(back_populates="positions")
    strategy: Mapped["Strategy"] = relationship()


class LogEntry(Base):
    """Curated business events (position open/close, errors, cycle
    summaries, skip_* decisions) for the future API/UI -- NOT a mirror of
    the full per-tick/debug volume, which goes to stdout instead (see the
    multi-account-architecture memory's "Logging split" note). All four FKs
    are nullable: a system/scheduler-level event may have none of them, a
    strategy-level event may have no position, etc.

    `payload` must never contain decrypted credentials or other secrets --
    plain JSON (not encrypted), same as the structured fields
    utils/trade_logger.StrategyLogger already writes to its JSONL files.
    """
    __tablename__ = "logs"
    __table_args__ = (
        Index("ix_logs_account_ts", "account_id", "ts"),
        Index("ix_logs_strategy_ts", "strategy_id", "ts"),
        Index("ix_logs_level_ts", "level", "ts"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)

    # level/kind: plain strings by design (see the "DB schema conventions"
    # memory) -- validated against webapp/schemas/enums.py's LogLevel/LogKind
    # before any write, not enforced at the DB level.
    level: Mapped[str] = mapped_column(String(16), default="info")
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    _payload: Mapped[str | None] = mapped_column("payload", Text, nullable=True)
    cycle_id: Mapped[str | None] = mapped_column(String(32), nullable=True)

    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    account_id: Mapped[int | None] = mapped_column(ForeignKey("accounts.id"), nullable=True)
    strategy_id: Mapped[int | None] = mapped_column(ForeignKey("strategies.id"), nullable=True)
    position_id: Mapped[int | None] = mapped_column(ForeignKey("positions.id"), nullable=True)

    user: Mapped["User"] = relationship()
    account: Mapped["Account"] = relationship()
    strategy: Mapped["Strategy"] = relationship()
    position: Mapped["Position"] = relationship()

    @property
    def payload(self) -> dict | None:
        return json.loads(self._payload) if self._payload else None

    @payload.setter
    def payload(self, value: dict | None):
        self._payload = json.dumps(value) if value else None
