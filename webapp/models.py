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


class Asset(Base):
    """Canonical instrument symbol used inside strategy code (GER40, XAUUSD,
    BTCUSDT, ...) -- the name strategies/backtests reason about, independent
    of what any given broker happens to call it. Broker-specific ticker
    strings live in `BrokerAssetSymbol`, not here (ALGODEV-30/31: this table
    replaces the previously-hardcoded `SYMBOL_CANDIDATES` guess-lists
    scattered across bot/s0XX_config.py once a resolver is wired in).

    Seeded (see webapp/cli.py's `seed-assets`) from every symbol actually
    referenced by a strategy or backtest in this repo as of 2026-08-21 --
    real, code-derived entries only, never invented. Several strategies
    reference the SAME underlying instrument under different source-specific
    names (e.g. fvg_mtf's OANDA-style `DAX30M` vs S007's live `GER40`, both
    the German DAX) -- those collapse to one canonical row here, with the
    alternate names recorded in `notes`, rather than one row per naming
    variant."""
    __tablename__ = "assets"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    asset_class: Mapped[str] = mapped_column(String(24), nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Broker(Base):
    """A specific broker/exchange/prop-firm entity (IC Markets, Bybit, FTMO,
    ...) that an Account is actually held at -- distinct from Account.broker,
    which names the PLATFORM (CTRADER/BYBIT) it connects through, since
    several brokers can share a platform. `is_prop_firm` distinguishes a
    prop-firm evaluation/funded account from a plain retail broker/exchange
    rather than using a separate entity type -- both need the same
    algo/API-trading policy fields, prop firms just also populate the
    prop-specific ones.

    Only IC Markets and Bybit are seeded here (migration 006) -- the real,
    already-connected brokers behind the live S007/S009/S011 accounts
    (`brokerName=icmarketssc` confirmed live via ProtoOATraderReq
    2026-08-19/21), needed structurally to backfill Account.broker_id.
    Deliberately NOT seeded with prop-firm policy data (daily loss cap, max
    drawdown, profit split, evaluation type per FTMO/The5ers/etc.) -- that
    needs real researched values from claude/prompt-prop-firm-symbol-mapping.md,
    not fabricated here; add real broker rows via a future seed command once
    that data is available (Anton's explicit call, 2026-08-21)."""
    __tablename__ = "brokers"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    is_prop_firm: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # Comma-separated platform names this broker offers (CTRADER, MT5, ...).
    platforms: Mapped[str | None] = mapped_column(String(64), nullable=True)
    algo_allowed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    # Prop-specific policy fields -- nullable, meaningless for a plain
    # retail broker (is_prop_firm=False leaves these null, not zero/fake).
    daily_loss_cap_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_drawdown_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    profit_split_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    evaluation_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    policy_source: Mapped[str | None] = mapped_column(String(255), nullable=True)
    policy_checked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="active", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class BrokerAssetSymbol(Base):
    """(broker, asset, platform) -> the actual ticker string that broker
    uses on that platform (e.g. IC Markets/CTRADER's GER40 might be
    "GER40.cash"). Starts empty by design -- filled in only as each broker
    is actually connected to and a symbol verified against its real symbol
    list (ProtoOASymbolsListReq or equivalent), never guessed ahead of time.
    This is what ALGODEV-31's resolver will read once it exists; that
    ticket stays blocked until at least one real verified row lands here."""
    __tablename__ = "broker_asset_symbols"
    __table_args__ = (UniqueConstraint("broker_id", "asset_id", "platform",
                                       name="uq_broker_asset_platform"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    broker_id: Mapped[int] = mapped_column(ForeignKey("brokers.id"), nullable=False)
    asset_id: Mapped[int] = mapped_column(ForeignKey("assets.id"), nullable=False)
    platform: Mapped[str] = mapped_column(String(16), nullable=False)
    broker_symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)


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
    # Human-readable broker account number (cTrader calls this the "login",
    # e.g. 10101224; for Bybit, unused for now). Purely cosmetic -- shown to
    # the user so they can tell accounts apart at a glance in their own
    # broker app. NEVER used for API auth/routing -- that's
    # external_account_id (cTrader's ctidTraderAccountId), a wholly separate
    # numbering space with no conversion formula between the two (confirmed
    # live 2026-08-19: distinct ctids for the same account number, and vice
    # versa is possible in principle).
    broker_account_number: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # The specific broker/prop-firm entity this account is held at (IC
    # Markets, Bybit, FTMO, ...) -- distinct from the `broker` string above,
    # which names the PLATFORM (CTRADER/BYBIT) an account connects through.
    # Several brokers can share a platform (multiple prop firms all offer
    # cTrader), so this FK is what actually identifies who holds the money
    # and what their policy is (see the `brokers` table). NOT NULL: every
    # account must be attributable to a real broker entity, no unknown case.
    broker_id: Mapped[int] = mapped_column(ForeignKey("brokers.id"), nullable=False)
    env: Mapped[str] = mapped_column(String(16), nullable=False)
    label: Mapped[str] = mapped_column(String(64), default="")
    broker_host: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # broker credentials: one encrypted JSON blob, shape depends on `broker`
    # (see webapp/schemas/accounts.py's CREDENTIALS_BY_BROKER) -- set/read via
    # the `credentials` property, never the raw _enc column.
    _credentials_enc: Mapped[str | None] = mapped_column("credentials_enc", Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    user: Mapped["User"] = relationship(back_populates="accounts")
    broker_entity: Mapped["Broker"] = relationship()
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
    # "off" (shadow, default) | "dry" (compute+log intended orders, no broker
    # calls) | "execute" (place real orders) -- see webapp/schemas/enums.py's
    # BrokerMode. Per-(account,strategy) so flipping ONE link to live never
    # silently promotes a second account running the same strategy (e.g.
    # S009 has two BYBIT accounts registered; only one is meant to trade for
    # real). A worker further gates real orders on Account.env == "mainnet"
    # (see webapp/runner.py::_worker_s009) -- broker_mode alone is not
    # enough to reach mainnet on a demo/testnet account.
    broker_mode: Mapped[str] = mapped_column(String(16), default="off")

    # runtime status (updated by the runner each cycle; UI reads it)
    status: Mapped[str] = mapped_column(String(32), default="idle")
    last_cycle_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    account: Mapped["Account"] = relationship(back_populates="strategy_links")
    strategy: Mapped["Strategy"] = relationship(back_populates="account_links")


class StrategyState(Base):
    """Generic cross-cycle persistent state for a strategy that needs one --
    e.g. S009's daily target book/equity/last-booked-day, which used to live
    in a single file (reports/paper_s009/state.json) shared by every
    account, making a second enabled S009 account collide with the first.
    One JSON blob per (account, strategy) pair, keyed by account_strategy_id
    so it moves and deletes with that row. NOT every strategy needs this --
    S007 carries no state between cycles (it re-derives everything from the
    broker + its own event log each time) -- rows exist only for strategies
    whose worker actually calls webapp/state_store.py's DBStateStore.

    DB-backed rather than a file so it survives an ephemeral container
    (Docker/k8s CronJob has no guaranteed persistent local disk between
    runs, unlike the long-lived launchd/VM setup files used historically).
    """
    __tablename__ = "strategy_state"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_strategy_id: Mapped[int] = mapped_column(
        ForeignKey("account_strategies.id"), unique=True, nullable=False)
    _state: Mapped[str] = mapped_column("state_json", Text, nullable=False, default="{}")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    account_strategy: Mapped["AccountStrategy"] = relationship()

    @property
    def state(self) -> dict:
        return json.loads(self._state) if self._state else {}

    @state.setter
    def state(self, value: dict):
        self._state = json.dumps(value)


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
