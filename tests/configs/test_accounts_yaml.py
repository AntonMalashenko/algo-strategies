"""Validity checks for the on-disk configs/accounts.yml(.example) files
themselves -- distinct from tests/bot/test_accounts_config.py, which tests
bot/accounts_config.py's lookup *logic* against a controlled fixture and
therefore never touches these real files.

Regression coverage for a real incident (2026-07-29): configs/accounts.yml
started with a stray leading space before the top-level `CTRADER:` key,
which is invalid YAML at document-root indentation. bot.accounts_config's
_load() propagated the resulting yaml.ParserError, which bot/s007_paper.py
caught and logged per-cycle -- so the bot silently did nothing for the
first ~4 minutes of every live session that day instead of failing loudly
once at startup. Nothing validated the file's syntax/shape ahead of time.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent.parent
EXAMPLE_PATH = ROOT / "configs" / "accounts.yml.example"
REAL_PATH = ROOT / "configs" / "accounts.yml"

REQUIRED_CTRADER_FIELDS = {"username", "CLIENT_ID", "CLIENT_SECRET", "ACCOUNT_ID", "ACCESS_TOKEN"}
REQUIRED_BYBIT_FIELDS = {"username", "API_KEY", "API_SECRET", "TESTNET"}


def _load_yaml(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _assert_valid_shape(data: dict, source: str) -> None:
    assert isinstance(data, dict), f"{source}: top-level document must be a mapping"

    for broker, required_fields in (("CTRADER", REQUIRED_CTRADER_FIELDS),
                                     ("BYBIT", REQUIRED_BYBIT_FIELDS)):
        if broker not in data:
            continue
        rows = data[broker]
        assert isinstance(rows, list), f"{source}: {broker} must be a list of entries"
        for i, row in enumerate(rows):
            assert isinstance(row, dict), f"{source}: {broker}[{i}] must be a mapping"
            missing = required_fields - row.keys()
            assert not missing, f"{source}: {broker}[{i}] missing fields {missing}"
            assert row.get("username"), f"{source}: {broker}[{i}] has an empty username"


def test_example_file_is_valid_yaml():
    # Would have caught the incident directly: a stray leading space before a
    # top-level key breaks yaml.safe_load entirely (ParserError), not just
    # one field.
    data = _load_yaml(EXAMPLE_PATH)
    assert data is not None


def test_example_file_has_expected_shape():
    _assert_valid_shape(_load_yaml(EXAMPLE_PATH), source=str(EXAMPLE_PATH))


@pytest.mark.skipif(not REAL_PATH.exists(), reason="configs/accounts.yml is gitignored/local-only")
def test_real_accounts_yml_is_valid_yaml():
    data = _load_yaml(REAL_PATH)
    assert data is not None


@pytest.mark.skipif(not REAL_PATH.exists(), reason="configs/accounts.yml is gitignored/local-only")
def test_real_accounts_yml_has_expected_shape():
    _assert_valid_shape(_load_yaml(REAL_PATH), source=str(REAL_PATH))
