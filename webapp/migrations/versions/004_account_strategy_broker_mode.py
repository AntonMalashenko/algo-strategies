"""account_strategies: broker_mode (per-link off/dry/execute gate)

Adds AccountStrategy.broker_mode -- how a worker is allowed to touch the
broker for THIS ONE (account, strategy) link: "off" (shadow, default),
"dry" (compute+log intended orders, no broker calls), "execute" (place
real orders). See webapp/schemas/enums.py::BrokerMode.

Needed to migrate S009 off its legacy launchd path (scripts/s009_tick.py ->
bot/s009_paper.py --broker execute --allow-mainnet, driving the real
Bybit-algo009 mainnet account) onto the DB-driven Docker/Ofelia path
(webapp/runner.py::_worker_s009), which previously hardcoded broker="off"
for every account unconditionally. A per-link column (not a global flip)
means enabling real execution for Bybit-algo009 can never silently also
promote a second S009 account (Bybit-tradebot1 is also registered, and
must stay shadow unless someone explicitly flips its own row).

Default "off" for every existing row -- this migration itself changes no
account's live trading behavior; a separate one-off script sets
broker_mode="execute" on the specific account_strategy row that already
traded for real via the legacy path.

Revision ID: 004
Revises: 003
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "004"
down_revision: Union[str, Sequence[str], None] = "003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("account_strategies") as batch:
        batch.add_column(
            sa.Column("broker_mode", sa.String(length=16), nullable=False,
                     server_default="off"))


def downgrade() -> None:
    with op.batch_alter_table("account_strategies") as batch:
        batch.drop_column("broker_mode")
