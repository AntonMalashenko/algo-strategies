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
        name: <optional label, e.g. "Bybit-algo009">
        API_KEY: ...
        API_SECRET: ...
        TESTNET: false
        active: true
        initial_balance: 100

Lookup, in order: `name` (an exact label match — the only way to pick one of
several sub-accounts that share the same `username`, e.g. one person running
more than one Bybit sub-account for different strategies) > `username` (works
ONLY while exactly one active row has that username — see below) > no
selector at all (works ONLY while exactly one active row exists for the
broker, full stop).

**Multiple active rows under the same `username` are a real, expected shape**
(one person, several broker sub-accounts) — `username` alone cannot disambiguate
those, by design (it identifies the *owner*, not the *account*). A caller in
that situation MUST pass `name`; passing only `username` (or nothing) resolves
to {} (ambiguous) rather than silently picking one — silently picking the
first row previously caused a real bug (2026-08-06): a second BYBIT entry was
added for S009's dedicated account, and the S009 bot's `BybitExec()` call
(no username, no name) started resolving to {} on the yml side and falling
back to the unrelated `.env` mainnet keys instead of erroring loudly. Missing
file / missing entry / ambiguous match all return {} — callers decide the
fallback (env vars, error), this module never crashes or guesses.
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


def _pick(broker: str, username: str | None = None, name: str | None = None) -> dict | None:
    """Resolve one active row, or None if there isn't exactly one unambiguous
    match. `name` (an entry's own `name:` label) takes priority when given —
    it is the only selector that can tell apart two active rows that share
    the same `username`. `username` alone only resolves when it matches
    exactly one active row; if it matches more than one (several sub-accounts
    for the same person), that is ambiguous on purpose — see module docstring
    for why silently picking the first row is exactly the bug this guards
    against."""
    rows = _active_entries(broker)
    if name:
        return next((r for r in rows if r.get("name") == name), None)
    if username:
        matches = [r for r in rows if r.get("username") == username]
        return matches[0] if len(matches) == 1 else None
    return rows[0] if len(rows) == 1 else None


def ctrader_creds(username: str | None = None, name: str | None = None) -> dict[str, str | int | None]:
    """CTRADER credentials for `name` (exact label) or `username` (only if it
    resolves to exactly one active row), or the sole active entry if both are
    omitted and unambiguous. Returns {} if nothing matches unambiguously."""
    row = _pick("CTRADER", username, name)
    if not row:
        return {}
    return dict(
        client_id=row.get("CLIENT_ID"),
        client_secret=row.get("CLIENT_SECRET"),
        access_token=row.get("ACCESS_TOKEN"),
        account_id=row.get("ACCOUNT_ID"),
    )


def bybit_creds(username: str | None = None, name: str | None = None) -> dict[str, str | bool | None]:
    """BYBIT credentials for `name` (exact label) or `username` (only if it
    resolves to exactly one active row), or the sole active entry if both are
    omitted and unambiguous. Returns {} if nothing matches unambiguously."""
    row = _pick("BYBIT", username, name)
    if not row:
        return {}
    return dict(
        api_key=row.get("API_KEY"),
        api_secret=row.get("API_SECRET"),
        testnet=row.get("TESTNET"),
    )
