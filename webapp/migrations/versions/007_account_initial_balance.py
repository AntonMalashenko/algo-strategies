"""accounts: initial_balance (deposited capital, account-level)

Adds Account.initial_balance -- how much capital has actually been deposited
into a broker account, e.g. for account-wide ROI/equity-curve tracking.
Distinct from the pre-existing AccountStrategy.initial_balance (per-
(account,strategy) seed/risk-cap reference, e.g. S007's day-scoped $ risk
cap) -- an account can run several strategies with independent capital
allocations, so that one stays on the association row. Found live
2026-09-05: Anton deposited into the S009 Bybit account and set the deposit
amount on account_strategies.initial_balance (the only field that existed),
which is unused by S009's runner and conceptually the wrong place for an
account-level fact anyway.

Revision ID: 007
Revises: 006
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "007"
down_revision: Union[str, Sequence[str], None] = "006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("accounts") as batch:
        batch.add_column(sa.Column("initial_balance", sa.Float(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("accounts") as batch:
        batch.drop_column("initial_balance")
