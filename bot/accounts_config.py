"""Broker credentials from configs/accounts.yml — single source of truth for
CTRADER/BYBIT credentials shared by bot/, webapp/, and scripts/.

configs/accounts.yml is gitignored (see .gitignore); configs/accounts.yml.example
is the tracked template. Never log the values this module returns.

Shape:
    CTRADER:
      - username: <login, matches webapp User.username>
        CLIENT_ID: ...
        CLIENT_SECRET: ...
        ACCOUNT_ID: ...
        ACCESS_TOKEN: ...
        active: true
        initial_balance: 10000
    BYBIT:
      - username: <login>
        API_KEY: ...
        API_SECRET: ...
        TESTNET: false
        active: true
        initial_balance: 100

Lookup is by `username` when given (multi-account case — the webapp runner
passes the owning User.username). With no username and exactly one active
entry for the broker, that entry is used (single-account scripts/CLI case).
Missing file / missing entry returns {} — callers decide the fallback
(env vars, error), they never crash here.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ACCOUNTS_CONFIG_PATH = ROOT / "configs" / "accounts.yml"
ACCOUNTS_CONFIG_PATH_ENV = "ACCOUNTS_CONFIG_PATH"  # override, e.g. for tests

_cache: dict[str, Any] | None = None
_cache_path: Path | None = None


def _config_path() -> Path:
    override = os.environ.get(ACCOUNTS_CONFIG_PATH_ENV)
    return Path(override) if override else DEFAULT_ACCOUNTS_CONFIG_PATH


def _load() -> dict:
    """Parse configs/accounts.yml once per path; re-parses if the env override
    changes (keeps tests that swap ACCOUNTS_CONFIG_PATH mid-process correct)."""
    global _cache, _cache_path
    path = _config_path()
    if _cache is not None and _cache_path == path:
        return _cache
    if not path.exists():
        _cache, _cache_path = {}, path
        return _cache
    with open(path, encoding="utf-8") as f:
        _cache = yaml.safe_load(f) or {}
    _cache_path = path
    return _cache


def _active_entries(broker: str) -> list[dict]:
    rows = _load().get(broker) or []
    return [r for r in rows if r.get("active", True)]


def _pick(broker: str, username: str | None) -> dict | None:
    rows = _active_entries(broker)
    if username:
        return next((r for r in rows if r.get("username") == username), None)
    return rows[0] if len(rows) == 1 else None  # ambiguous multi-entry: caller must pass username


def ctrader_creds(username: str | None = None) -> dict[str, str | int | None]:
    """CTRADER credentials for `username`, or the sole active entry if omitted
    and unambiguous. Returns {} if nothing matches."""
    row = _pick("CTRADER", username)
    if not row:
        return {}
    return dict(
        client_id=row.get("CLIENT_ID"),
        client_secret=row.get("CLIENT_SECRET"),
        access_token=row.get("ACCESS_TOKEN"),
        account_id=row.get("ACCOUNT_ID"),
    )


def bybit_creds(username: str | None = None) -> dict[str, str | bool | None]:
    """BYBIT credentials for `username`, or the sole active entry if omitted
    and unambiguous. Returns {} if nothing matches."""
    row = _pick("BYBIT", username)
    if not row:
        return {}
    return dict(
        api_key=row.get("API_KEY"),
        api_secret=row.get("API_SECRET"),
        testnet=row.get("TESTNET"),
    )
