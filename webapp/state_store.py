"""DB-backed state store -- the multi-account counterpart to
utils/strategy_state.py::FileStateStore, same `.load()`/`.save(dict)`
shape, backed by the `strategy_state` table (webapp/models.py) instead of a
file. One row per account_strategy_id, created lazily on first `.save()`.

Used by webapp/runner.py's per-strategy workers (e.g. S009's) so each
enabled account gets its own persisted book/equity/last-booked-day instead
of colliding on one shared file, and so state survives an ephemeral
container (no guaranteed local disk between Docker/k8s CronJob runs).
"""
from __future__ import annotations

from datetime import datetime

from webapp.models import StrategyState


class DBStateStore:
    def __init__(self, account_strategy_id: int, session):
        self.account_strategy_id = account_strategy_id
        self.session = session

    def _row(self) -> StrategyState | None:
        return (self.session.query(StrategyState)
                .filter_by(account_strategy_id=self.account_strategy_id).one_or_none())

    def load(self) -> dict:
        row = self._row()
        return row.state if row is not None else {}

    def save(self, state: dict) -> None:
        row = self._row()
        if row is None:
            row = StrategyState(account_strategy_id=self.account_strategy_id)
            self.session.add(row)
        row.state = state
        row.updated_at = datetime.utcnow()
        self.session.commit()
