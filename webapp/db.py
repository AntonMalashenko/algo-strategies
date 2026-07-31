"""Database engine/session (SQLAlchemy). SQLite by default; swap APP_DB_URL for
Postgres later with no model changes."""
from __future__ import annotations

import os
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

ROOT = Path(__file__).resolve().parent.parent
# Resolved against the repo root, not the process CWD -- a bare relative
# "sqlite:///data/app.db" default resolves relative to wherever the process
# happens to be invoked from. Incident 2026-07-28: running Alembic from
# webapp/ silently pointed at webapp/data/app.db instead of the repo root's
# data/app.db. See tests/webapp/test_db.py for the CWD-independence regression
# tests. APP_DB_URL, when set, is still used verbatim (no resolution applied).
DEFAULT_DB_PATH = ROOT / "data" / "app.db"
DB_URL = os.environ.get("APP_DB_URL", f"sqlite:///{DEFAULT_DB_PATH}")

_connect_args = {"check_same_thread": False} if DB_URL.startswith("sqlite") else {}
engine = create_engine(DB_URL, connect_args=_connect_args, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
Base = declarative_base()


def init_db() -> None:
    """Create tables. Safe to call repeatedly."""
    if DB_URL.startswith("sqlite:///"):
        path = DB_URL.replace("sqlite:///", "", 1)
        if path not in (":memory:", ""):
            Path(path).resolve().parent.mkdir(parents=True, exist_ok=True)
    from webapp import models  # noqa: F401  (register mappers)
    Base.metadata.create_all(engine)


def get_session():
    """Context-manager friendly session factory."""
    return SessionLocal()
