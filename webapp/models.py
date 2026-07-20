"""ORM models: users (login + cTrader creds), accounts (1 user -> many), positions.

Secrets (access token, optional client secret) are stored ENCRYPTED — set/read via
the helper properties, never the raw *_enc columns.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (Boolean, DateTime, Float, ForeignKey, Integer, String,
                        Text, UniqueConstraint)
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

    # cTrader credentials. client_id/secret optional (fall back to the platform's
    # global Open API app in env); access_token is the user's OAuth token.
    ctrader_client_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    _client_secret_enc: Mapped[str | None] = mapped_column("client_secret_enc", Text, nullable=True)
    _access_token_enc: Mapped[str | None] = mapped_column("access_token_enc", Text, nullable=True)

    accounts: Mapped[list["Account"]] = relationship(
        back_populates="user", cascade="all, delete-orphan")

    # --- encrypted secret accessors ---
    @property
    def client_secret(self) -> str | None:
        return decrypt_secret(self._client_secret_enc)

    @client_secret.setter
    def client_secret(self, value: str | None):
        self._client_secret_enc = encrypt_secret(value)

    @property
    def access_token(self) -> str | None:
        return decrypt_secret(self._access_token_enc)

    @access_token.setter
    def access_token(self, value: str | None):
        self._access_token_enc = encrypt_secret(value)


class Account(Base):
    __tablename__ = "accounts"
    __table_args__ = (UniqueConstraint("user_id", "ctid_trader_account_id",
                                       name="uq_user_account"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    ctid_trader_account_id: Mapped[int] = mapped_column(Integer, nullable=False)
    label: Mapped[str] = mapped_column(String(64), default="")
    is_live: Mapped[bool] = mapped_column(Boolean, default=False)
    host: Mapped[str | None] = mapped_column(String(64), nullable=True)  # override demo/live host

    # control + strategy config
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)        # start/stop switch
    strategy: Mapped[str] = mapped_column(String(16), default="S007")
    preset: Mapped[str] = mapped_column(String(32), default="BASELINE_S007")
    symbol: Mapped[str | None] = mapped_column(String(32), nullable=True)  # resolved/forced symbol
    risk_pct: Mapped[float] = mapped_column(Float, default=0.25)
    fixed_lot: Mapped[float] = mapped_column(Float, default=0.01)
    use_fixed_lot: Mapped[bool] = mapped_column(Boolean, default=True)

    # runtime status (updated by the runner each cycle; UI reads it)
    status: Mapped[str] = mapped_column(String(32), default="idle")
    last_cycle_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    user: Mapped["User"] = relationship(back_populates="accounts")
    positions: Mapped[list["Position"]] = relationship(
        back_populates="account", cascade="all, delete-orphan")


class Position(Base):
    """Bot-tracked position lifecycle (mirrors the file log; here for the UI)."""
    __tablename__ = "positions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), nullable=False)
    label: Mapped[str] = mapped_column(String(64), nullable=False)      # engine label (unique/day)
    side: Mapped[str] = mapped_column(String(4))
    entry: Mapped[float] = mapped_column(Float)
    sl: Mapped[float] = mapped_column(Float)
    tp: Mapped[float] = mapped_column(Float)
    is_add: Mapped[bool] = mapped_column(Boolean, default=False)
    broker_position_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="open")     # open/closed
    reason: Mapped[str | None] = mapped_column(String(32), nullable=True)  # target/flat/...
    opened_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    account: Mapped["Account"] = relationship(back_populates="positions")
