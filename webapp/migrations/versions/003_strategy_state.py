"""strategy_state: generic cross-cycle persistent state per (account, strategy)

Adds a home for state a strategy needs to carry between scheduled cycles
(e.g. S009's daily target book/equity/last-booked-day), one JSON blob keyed
by account_strategy_id. Replaces the single shared
reports/paper_s009/state.json file, which collides as soon as a second S009
account is enabled, and would not survive an ephemeral container anyway.

Not every strategy needs a row here -- S007 carries no state between
cycles. See webapp/models.py::StrategyState and webapp/state_store.py.

Revision ID: 003
Revises: 002
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "003"
down_revision: Union[str, Sequence[str], None] = "002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "strategy_state",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("account_strategy_id", sa.Integer(), nullable=False),
        sa.Column("state_json", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["account_strategy_id"], ["account_strategies.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("account_strategy_id"),
    )


def downgrade() -> None:
    op.drop_table("strategy_state")
