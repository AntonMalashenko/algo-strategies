"""Shared Redis access for the whole project -- one connection helper for
both roles the single `redis` service in docker-compose.yml serves:

  * STATE_DB (0): durable shared state / distributed locks / pub-sub
  * CACHE_DB (1): market-data cache (e.g. the per-tick M1-bar cache parked
    in docs/DEV_PLANS.md). Cache callers set their OWN per-key TTL -- the
    server runs `--maxmemory-policy noeviction`, so nothing is evicted for
    them automatically.

Connection target = the REDIS_URL env var, set to `redis://redis:6379` on
the Ofelia job-run worker containers (docker-compose.yml); the hostname
`redis` resolves on the `algo` compose network. Unset -> DEFAULT_REDIS_URL,
the same value, so a manual `python -m ...` run inside a worker container
still works with no extra setup.

Redis is NOT published to the host (Anton's call, 2026-09-08). A process
running directly on the macOS host (the manually-started webapp) therefore
cannot reach it: `is_available()` returns False there. Treat Redis as an
optional accelerator, never a hard dependency -- guard every use with
`is_available()` (or catch and fall back) so a wedged/absent Redis can
never break a trading cycle.
"""
from __future__ import annotations

import os
from functools import lru_cache

try:
    import redis as _redis
except ModuleNotFoundError:                      # research venv: not in core requirements.txt
    _redis = None

REDIS_URL_ENV = "REDIS_URL"
DEFAULT_REDIS_URL = "redis://redis:6379"

# Logical DB split on the one shared instance -- see the module docstring.
STATE_DB = 0
CACHE_DB = 1

# Fail fast rather than block a trading cycle on an optional cache: a
# worker's whole tick budget is ~120s (scripts/scheduler_tick.py's
# ITEM_TIMEOUT_SECONDS) and must never be spent stuck on Redis.
CONNECT_TIMEOUT_SECONDS = 2.0
OP_TIMEOUT_SECONDS = 2.0
HEALTH_CHECK_INTERVAL_SECONDS = 30


def redis_url() -> str:
    """The configured connection URL (env override, else the default)."""
    return os.environ.get(REDIS_URL_ENV) or DEFAULT_REDIS_URL


@lru_cache(maxsize=None)
def get_client(db: int = STATE_DB, *, decode_responses: bool = True):
    """A pooled ``redis.Redis`` for logical database ``db``, cached per
    (db, decode_responses) so all callers in a process share one pool.

    Raises RuntimeError if the ``redis`` package is not installed (it is in
    requirements-docker.txt, not requirements.txt's core stack). Does NOT
    connect here -- redis-py connects lazily on first command.
    """
    if _redis is None:
        raise RuntimeError(
            "the 'redis' package is not installed in this environment "
            "(present in requirements-docker.txt, not requirements.txt core)"
        )
    return _redis.Redis.from_url(
        redis_url(),
        db=db,
        decode_responses=decode_responses,
        socket_connect_timeout=CONNECT_TIMEOUT_SECONDS,
        socket_timeout=OP_TIMEOUT_SECONDS,
        health_check_interval=HEALTH_CHECK_INTERVAL_SECONDS,
    )


def is_available(db: int = STATE_DB) -> bool:
    """True iff Redis answers PING within the timeout. Never raises -- this
    is the guard for the "use Redis if it's there, fall back if not"
    pattern every caller must follow.
    """
    if _redis is None:
        return False
    try:
        return bool(get_client(db).ping())
    except Exception:                             # noqa: BLE001 -- optional dep, any failure == unavailable
        return False
