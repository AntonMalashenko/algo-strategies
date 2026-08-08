"""position broker fill data (exit price, volume, pnl components, origin)

Adds the columns webapp/sync_positions.py fills in from the broker, so the DB
stops being "what the bot intended" and becomes "what actually happened":

  origin          bot | adopted -- a position the sync found at the broker that
                  this bot never opened (manual trade, older bot) is stored as
                  'adopted' rather than silently blended in with bot trades.
  exit_price      broker fill price of the closing deal
  volume_lots     closed volume, in lots
  gross_profit / swap / commission / pnl
                  the broker's own money figures in the deposit currency;
                  pnl is their sum (swap and commission arrive already signed).
  broker_deal_id  the closing deal, so a re-sync is idempotent
  synced_at       when the sync last touched this row

Every column is nullable on purpose: NULL means "not synced / broker cannot
report it", which is NOT the same as 0.0. Anything aggregating pnl must skip
nulls instead of coercing them, or unsynced trades read as break-even.

Revision ID: 002
Revises: 001
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "002"
down_revision: Union[str, Sequence[str], None] = "001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_NEW_COLUMNS = [
    sa.Column("origin", sa.String(length=16), nullable=False, server_default="bot"),
    sa.Column("exit_price", sa.Float(), nullable=True),
    sa.Column("volume_lots", sa.Float(), nullable=True),
    sa.Column("gross_profit", sa.Float(), nullable=True),
    sa.Column("swap", sa.Float(), nullable=True),
    sa.Column("commission", sa.Float(), nullable=True),
    sa.Column("pnl", sa.Float(), nullable=True),
    sa.Column("broker_deal_id", sa.BigInteger(), nullable=True),
    sa.Column("synced_at", sa.DateTime(), nullable=True),
]


def upgrade() -> None:
    # batch_alter_table: SQLite cannot ALTER TABLE ADD COLUMN with every
    # constraint shape, so alembic rebuilds the table there. On Postgres this
    # is a plain ADD COLUMN each.
    with op.batch_alter_table("positions") as batch:
        for col in _NEW_COLUMNS:
            batch.add_column(col)
        # cTrader declares positionId as int64. Postgres INTEGER tops out at
        # 2^31; today's ids fit, but a column that silently overflows years
        # from now is not worth keeping while we are already rebuilding the
        # table. SQLite ignores the width entirely.
        batch.alter_column("broker_position_id",
                           existing_type=sa.Integer(), type_=sa.BigInteger(),
                           existing_nullable=True)
    op.create_index("ix_positions_acct_strat_status", "positions",
                    ["account_id", "strategy_id", "status"])
    op.create_index("ix_positions_broker_position_id", "positions", ["broker_position_id"])


def downgrade() -> None:
    op.drop_index("ix_positions_broker_position_id", table_name="positions")
    op.drop_index("ix_positions_acct_strat_status", table_name="positions")
    with op.batch_alter_table("positions") as batch:
        batch.alter_column("broker_position_id",
                           existing_type=sa.BigInteger(), type_=sa.Integer(),
                           existing_nullable=True)
        for col in reversed(_NEW_COLUMNS):
            batch.drop_column(col.name)
