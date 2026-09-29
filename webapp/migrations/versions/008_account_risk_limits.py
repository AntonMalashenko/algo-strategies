"""accounts: account-level risk limits; account_strategies: daily_risk_cap_pct

ALGODEV-55 (S007 + S021 on an FTMO account). Moves the risk knobs that were
code constants into the DB, per Anton's request:

- accounts.guard_daily_loss_pct / guard_max_loss_pct / day_reset_tz: our own
  buffered account-wide loss limits, enforced by bot/account_guard.py before
  every new entry of every strategy on the account.
- accounts.evaluation_phase / profit_target_pct: prop-evaluation bookkeeping
  (informational, read by no trading code).
- account_strategies.daily_risk_cap_pct: per-(account, strategy) daily risk
  budget; NULL keeps the strategy's config default (S007's
  DAILY_RISK_CAP_PCT), so existing rows behave exactly as before.

All columns nullable, no backfill -- NULL means "off / config default".

Revision ID: 008
Revises: 007
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "008"
down_revision: Union[str, Sequence[str], None] = "007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

ACCOUNT_COLUMNS = (
    ("guard_daily_loss_pct", sa.Float()),
    ("guard_max_loss_pct", sa.Float()),
    ("day_reset_tz", sa.String(32)),
    ("evaluation_phase", sa.String(32)),
    ("profit_target_pct", sa.Float()),
)


def upgrade() -> None:
    with op.batch_alter_table("accounts") as batch:
        for name, type_ in ACCOUNT_COLUMNS:
            batch.add_column(sa.Column(name, type_, nullable=True))
    with op.batch_alter_table("account_strategies") as batch:
        batch.add_column(sa.Column("daily_risk_cap_pct", sa.Float(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("account_strategies") as batch:
        batch.drop_column("daily_risk_cap_pct")
    with op.batch_alter_table("accounts") as batch:
        for name, _type in reversed(ACCOUNT_COLUMNS):
            batch.drop_column(name)
