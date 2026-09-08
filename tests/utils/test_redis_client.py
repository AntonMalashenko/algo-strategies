"""utils.redis_client -- the shared Redis connection helper. No live server
is assumed: these cover URL resolution and the never-raises `is_available()`
contract (both of which hold whether or not the `redis` package or a server
is present)."""
from __future__ import annotations

import importlib

from utils import redis_client


def test_redis_url_defaults_when_env_unset(monkeypatch):
    monkeypatch.delenv(redis_client.REDIS_URL_ENV, raising=False)
    assert redis_client.redis_url() == redis_client.DEFAULT_REDIS_URL


def test_redis_url_honours_env_override(monkeypatch):
    monkeypatch.setenv(redis_client.REDIS_URL_ENV, "redis://example:6380/2")
    assert redis_client.redis_url() == "redis://example:6380/2"


def test_state_and_cache_dbs_are_distinct():
    assert redis_client.STATE_DB != redis_client.CACHE_DB


def test_is_available_is_false_and_never_raises_without_a_server(monkeypatch):
    # Point at a host that cannot resolve/connect; must return False, not raise.
    monkeypatch.setenv(redis_client.REDIS_URL_ENV, "redis://127.0.0.1:6390/0")
    importlib.reload(redis_client)      # drop the lru_cache'd client bound to any prior URL
    try:
        assert redis_client.is_available() is False
    finally:
        monkeypatch.delenv(redis_client.REDIS_URL_ENV, raising=False)
        importlib.reload(redis_client)
