"""brokers/assets/broker_asset_symbols schema + Account.broker_id (ALGODEV-30)

Adds a structured, queryable place for broker/prop-firm identity and
broker-specific ticker symbols, replacing the plan to keep guessing symbols
via bot/s007_config.py's hardcoded SYMBOL_CANDIDATES forever:

* `assets` -- canonical instrument symbols used inside strategy code.
* `brokers` -- unified retail-broker/prop-firm entity (is_prop_firm flag,
  not a separate table), with algo/API policy fields.
* `broker_asset_symbols` -- (broker, asset, platform) -> real ticker string.
  Starts empty by design -- no fabricated symbol mappings.
* `accounts.broker_id` -- NOT NULL FK to `brokers`, backfilled below for the
  existing live S007 (accounts 1, 4 -- IC Markets/CTRADER, confirmed live
  via ProtoOATraderReq brokerName=icmarketssc) and S009 (accounts 2, 3 --
  Bybit) accounts before the NOT NULL constraint is applied, so this
  migration is safe to run against the already-populated production DB.

Deliberately NOT seeded here: prop-firm policy data (FTMO/The5ers/etc. --
daily loss cap, max drawdown, profit split, evaluation type). That needs
real researched values from claude/prompt-prop-firm-symbol-mapping.md, not
fabricated placeholders (Anton's explicit call, 2026-08-21) -- add real
broker rows via a future migration/seed command once that data is in hand.
The `assets` table IS seeded, separately, via `webapp.cli seed-assets`
(idempotent, code-derived symbol list) -- kept out of this migration so the
asset list can grow without a new migration each time.

Revision ID: 006
Revises: 005
"""
from datetime import datetime
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "006"
down_revision: Union[str, Sequence[str], None] = "005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "assets",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("symbol", sa.String(length=32), nullable=False, unique=True),
        sa.Column("asset_class", sa.String(length=24), nullable=False),
        sa.Column("notes", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime, nullable=False),
    )
    op.create_table(
        "brokers",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("name", sa.String(length=64), nullable=False, unique=True),
        sa.Column("is_prop_firm", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("platforms", sa.String(length=64), nullable=True),
        sa.Column("algo_allowed", sa.Boolean, nullable=True),
        sa.Column("daily_loss_cap_pct", sa.Float, nullable=True),
        sa.Column("max_drawdown_pct", sa.Float, nullable=True),
        sa.Column("profit_split_pct", sa.Float, nullable=True),
        sa.Column("evaluation_type", sa.String(length=32), nullable=True),
        sa.Column("policy_source", sa.String(length=255), nullable=True),
        sa.Column("policy_checked_at", sa.DateTime, nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="active"),
        sa.Column("created_at", sa.DateTime, nullable=False),
    )
    op.create_table(
        "broker_asset_symbols",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("broker_id", sa.Integer, sa.ForeignKey("brokers.id"), nullable=False),
        sa.Column("asset_id", sa.Integer, sa.ForeignKey("assets.id"), nullable=False),
        sa.Column("platform", sa.String(length=16), nullable=False),
        sa.Column("broker_symbol", sa.String(length=32), nullable=False),
        sa.Column("verified_at", sa.DateTime, nullable=True),
        sa.Column("notes", sa.Text, nullable=True),
        sa.UniqueConstraint("broker_id", "asset_id", "platform", name="uq_broker_asset_platform"),
    )

    # accounts.broker_id: add nullable, backfill, THEN enforce NOT NULL --
    # the only safe order against a table with existing rows.
    with op.batch_alter_table("accounts") as batch:
        batch.add_column(sa.Column(
            "broker_id", sa.Integer,
            sa.ForeignKey("brokers.id", name="fk_accounts_broker_id"), nullable=True))

    conn = op.get_bind()
    now = datetime.utcnow()
    ic_markets_id = conn.execute(
        sa.text("INSERT INTO brokers (name, is_prop_firm, platforms, status, created_at) "
                "VALUES ('IC Markets', 0, 'CTRADER', 'active', :now)"),
        {"now": now},
    ).lastrowid
    bybit_id = conn.execute(
        sa.text("INSERT INTO brokers (name, is_prop_firm, platforms, status, created_at) "
                "VALUES ('Bybit', 0, 'BYBIT', 'active', :now)"),
        {"now": now},
    ).lastrowid
    conn.execute(sa.text("UPDATE accounts SET broker_id = :bid WHERE broker = 'CTRADER'"),
                {"bid": ic_markets_id})
    conn.execute(sa.text("UPDATE accounts SET broker_id = :bid WHERE broker = 'BYBIT'"),
                {"bid": bybit_id})

    with op.batch_alter_table("accounts") as batch:
        batch.alter_column("broker_id", nullable=False)


def downgrade() -> None:
    with op.batch_alter_table("accounts") as batch:
        batch.drop_column("broker_id")
    op.drop_table("broker_asset_symbols")
    op.drop_table("brokers")
    op.drop_table("assets")
