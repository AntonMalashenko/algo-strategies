"""accounts: broker_account_number (human-readable account number, display-only)

Adds Account.broker_account_number -- the broker's own human-readable
account number (cTrader calls this the "login", e.g. 10101224), distinct
from external_account_id (cTrader's ctidTraderAccountId, e.g. 48354548 --
an unrelated numbering space the Open API uses for auth/routing). Found
live 2026-08-19: a manual diagnostic script conflated the two and wrongly
concluded two DB accounts pointed at the same broker account, when in fact
they didn't -- the ctid was always correct, there was just no human-
readable number stored anywhere to sanity-check it against. This column is
purely cosmetic (never read by runner.py / bot/ctrader.py for routing) so
operators can tell accounts apart at a glance without re-deriving the
mapping via a live API call.

Revision ID: 005
Revises: 004
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "005"
down_revision: Union[str, Sequence[str], None] = "004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("accounts") as batch:
        batch.add_column(sa.Column("broker_account_number", sa.String(length=32), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("accounts") as batch:
        batch.drop_column("broker_account_number")
