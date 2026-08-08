"""Tests for bot/accounts_config.py — configs/accounts.yml credential lookup.

Uses ACCOUNTS_CONFIG_PATH_ENV to point at a temp fixture so real secrets in
the repo's configs/accounts.yml are never touched or required.
"""
from __future__ import annotations

import importlib

import pytest

from bot import accounts_config as AC

FIXTURE_YAML = """\
CTRADER:
  - username: alice@example.com
    CLIENT_ID: cid-a
    CLIENT_SECRET: secret-a
    ACCOUNT_ID: 111
    ACCESS_TOKEN: token-a
    active: true
  - username: bob@example.com
    CLIENT_ID: cid-b
    CLIENT_SECRET: secret-b
    ACCOUNT_ID: 222
    ACCESS_TOKEN: token-b
    active: false

BYBIT:
  - username: alice@example.com
    API_KEY: bkey-a
    API_SECRET: bsecret-a
    TESTNET: true
    active: true
"""

# Two active BYBIT rows sharing one `username` -- the real shape that broke
# S009 on 2026-08-06 (a second sub-account added for the strategy's own $50
# account, alongside an existing one). `username` alone can no longer tell
# them apart; `name` must.
MULTI_BYBIT_FIXTURE_YAML = """\
BYBIT:
  - username: alice@example.com
    name: Bybit-primary
    API_KEY: bkey-primary
    API_SECRET: bsecret-primary
    TESTNET: false
    active: true
  - username: alice@example.com
    name: Bybit-strategy009
    API_KEY: bkey-009
    API_SECRET: bsecret-009
    TESTNET: false
    active: true
"""


@pytest.fixture
def fixture_config(tmp_path, monkeypatch):
    path = tmp_path / "accounts.yml"
    path.write_text(FIXTURE_YAML)
    monkeypatch.setenv(AC.ACCOUNTS_CONFIG_PATH_ENV, str(path))
    AC._cache, AC._cache_path = None, None      # bust the module-level cache
    yield path
    AC._cache, AC._cache_path = None, None


@pytest.fixture
def multi_bybit_config(tmp_path, monkeypatch):
    path = tmp_path / "accounts.yml"
    path.write_text(MULTI_BYBIT_FIXTURE_YAML)
    monkeypatch.setenv(AC.ACCOUNTS_CONFIG_PATH_ENV, str(path))
    AC._cache, AC._cache_path = None, None
    yield path
    AC._cache, AC._cache_path = None, None


def test_ctrader_creds_by_username(fixture_config):
    creds = AC.ctrader_creds("alice@example.com")
    assert creds == dict(client_id="cid-a", client_secret="secret-a",
                          access_token="token-a", account_id=111)


def test_ctrader_creds_inactive_entry_not_returned(fixture_config):
    # bob is active: false — must not resolve even though the username matches
    assert AC.ctrader_creds("bob@example.com") == {}


def test_ctrader_creds_unknown_username_returns_empty(fixture_config):
    assert AC.ctrader_creds("nobody@example.com") == {}


def test_ctrader_creds_no_username_ambiguous_when_multiple_rows(fixture_config):
    # two CTRADER rows in the fixture (one inactive) -> only one active, so
    # omitting username still resolves unambiguously to alice
    assert AC.ctrader_creds() == AC.ctrader_creds("alice@example.com")


def test_bybit_creds_by_username(fixture_config):
    creds = AC.bybit_creds("alice@example.com")
    assert creds == dict(api_key="bkey-a", api_secret="bsecret-a", testnet=True)


def test_missing_file_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setenv(AC.ACCOUNTS_CONFIG_PATH_ENV, str(tmp_path / "does-not-exist.yml"))
    AC._cache, AC._cache_path = None, None
    assert AC.ctrader_creds("anyone") == {}
    assert AC.bybit_creds("anyone") == {}
    AC._cache, AC._cache_path = None, None


def test_bybit_creds_two_rows_same_username_ambiguous_without_name(multi_bybit_config):
    # Two active rows share "alice@example.com" -- username alone must NOT
    # silently pick the first one (that was the 2026-08-06 bug). No selector
    # at all is ambiguous too.
    assert AC.bybit_creds("alice@example.com") == {}
    assert AC.bybit_creds() == {}


def test_bybit_creds_two_rows_resolved_by_name(multi_bybit_config):
    primary = AC.bybit_creds(name="Bybit-primary")
    strategy009 = AC.bybit_creds(name="Bybit-strategy009")
    assert primary == dict(api_key="bkey-primary", api_secret="bsecret-primary", testnet=False)
    assert strategy009 == dict(api_key="bkey-009", api_secret="bsecret-009", testnet=False)
    assert primary != strategy009


def test_bybit_creds_name_wins_even_if_username_also_passed(multi_bybit_config):
    # name is authoritative -- passing a (correct, but non-disambiguating)
    # username alongside it must not change the result.
    creds = AC.bybit_creds(username="alice@example.com", name="Bybit-strategy009")
    assert creds == dict(api_key="bkey-009", api_secret="bsecret-009", testnet=False)


def test_bybit_creds_unknown_name_returns_empty(multi_bybit_config):
    assert AC.bybit_creds(name="Bybit-does-not-exist") == {}
