"""Tests for webapp/db.py's DB_URL resolution.

Regression coverage for a real incident (2026-07-28): DB_URL's default
(`sqlite:///data/app.db`) was resolved relative to the process's CWD, so
running Alembic from webapp/ silently pointed at webapp/data/app.db instead
of the repo root's data/app.db. Fixed by resolving the default against the
repo root explicitly, regardless of invocation directory.

Each case spawns a fresh subprocess rather than monkeypatching os.environ +
importlib.reload in-process: DB_URL, engine, and Base are all computed once
at import time, and reloading webapp.db in-process would rebind Base to a
new declarative_base() disconnected from whatever already imported
webapp.models in this pytest session, corrupting ORM state for any other
test that runs afterwards.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
EXPECTED_DEFAULT = f"sqlite:///{ROOT / 'data' / 'app.db'}"


def _db_url(cwd: Path, extra_env: dict[str, str] | None = None) -> str:
    env = dict(os.environ)
    env.pop("APP_DB_URL", None)
    env.update(extra_env or {})
    env["PYTHONPATH"] = os.pathsep.join(
        p for p in (str(ROOT), env.get("PYTHONPATH")) if p
    )
    result = subprocess.run(
        [sys.executable, "-c", "from webapp.db import DB_URL; print(DB_URL)"],
        cwd=str(cwd), env=env, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def test_default_db_url_resolves_to_repo_root_from_repo_root():
    assert _db_url(ROOT) == EXPECTED_DEFAULT


def test_default_db_url_resolves_to_repo_root_from_webapp_dir():
    # The actual failure mode: `cd webapp && alembic ...` must still hit the
    # repo root's data/app.db, not webapp/data/app.db.
    assert _db_url(ROOT / "webapp") == EXPECTED_DEFAULT


def test_default_db_url_resolves_to_repo_root_from_arbitrary_cwd(tmp_path):
    assert _db_url(tmp_path) == EXPECTED_DEFAULT


def test_app_db_url_env_override_used_verbatim_regardless_of_cwd():
    custom = "sqlite:////tmp/custom-test.db"
    assert _db_url(ROOT / "webapp", {"APP_DB_URL": custom}) == custom
