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
    """Create tables. Safe to call repeatedly.

    No-ops if the target sqlite DB already has Alembic's version table. Once
    a database is under Alembic's control (webapp/migrations/), create_all()
    must never touch it: it would silently create any table/column Alembic
    hasn't been asked to add yet with no alembic_version bump, so a later
    `alembic upgrade head` either thinks it's already there and no-ops, or
    fails on a table that already exists. init_db() is called from several
    non-migration call sites (webapp/cli.py's create-user, add-account, ...;
    scripts/migrate_accounts_yml.py) purely to make sure tables exist for a
    fresh/throwaway DB -- those call sites must not be able to drift an
    Alembic-managed production DB out of sync with its migration history.
    Genuinely fresh DBs (tests, first bootstrap) have no alembic_version
    table yet, so create_all() still runs for them exactly as before; use
    `alembic upgrade head` (see webapp/cli.py's init-db docstring) for any
    schema change on a DB that already has one.
    """
    if DB_URL.startswith("sqlite:///"):
        path = DB_URL.replace("sqlite:///", "", 1)
        if path not in (":memory:", ""):
            resolved = Path(path).resolve()
            resolved.parent.mkdir(parents=True, exist_ok=True)
            if resolved.exists():
                import sqlite3
                conn = sqlite3.connect(str(resolved))
                try:
                    has_alembic = conn.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' "
                        "AND name='alembic_version'"
                    ).fetchone() is not None
                finally:
                    conn.close()
                if has_alembic:
                    return
    from webapp import models  # noqa: F401  (register mappers)
    Base.metadata.create_all(engine)


def get_session():
    """Context-manager friendly session factory."""
    return SessionLocal()
