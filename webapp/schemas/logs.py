"""LogEntry domain schema -- curated business events only (position open/
close, errors, cycle summaries, skip_* decisions), for the future API/UI.
Full per-tick debug/system volume goes to stdout instead, not this table —
see the multi-account-architecture memory's "Logging split" note.

`payload` must never contain decrypted credentials or other secrets -- it's
meant for the same kind of structured fields utils/trade_logger.StrategyLogger
already puts in its JSONL event files (stop_distance, risk_amount, sl/tp,
broker order results, ...), not raw account credentials.
"""
from __future__ import annotations

from pydantic import BaseModel

from webapp.schemas.enums import LogKind, LogLevel


class LogEntryCreate(BaseModel):
    level: LogLevel = LogLevel.INFO
    kind: LogKind
    message: str | None = None
    payload: dict | None = None
    cycle_id: str | None = None

    user_id: int | None = None
    account_id: int | None = None
    strategy_id: int | None = None
    position_id: int | None = None
