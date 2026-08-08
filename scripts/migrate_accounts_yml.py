"""One-off migration: import configs/accounts.yml into the webapp DB (User/
Account/Strategy/AccountStrategy). Going forward the DB is the source of
truth for users/accounts (see the multi-account-architecture memory) --
accounts.yml stays only as an import/bootstrap convenience and a fallback
credential source (bot/accounts_config.py).

Mapping (applied only when CREATING a new AccountStrategy link -- see below):
  CTRADER entries -> Strategy "S007" (broker CTRADER)
  BYBIT entries    -> Strategy "S009" (broker BYBIT)
  active           -> AccountStrategy.enabled
  initial_balance  -> AccountStrategy.initial_balance (seed for a paper/
                       shadow ledger, e.g. S009's -- NOT live balance, which
                       is always fetched fresh from the broker each cycle)
  env (CTRADER)    -> Account.env; REQUIRED field in accounts.yml, no
                       default guessed (demo vs live is not something to
                       infer silently)
  env (BYBIT)      -> prefers an explicit `env:` field if present, else
                       derived from TESTNET: true->testnet, false->mainnet
                       (Bybit's separate "demo" env isn't expressible by
                       that boolean alone)

One User row per unique `username`, shared across broker sections if the
same email appears in both (as in the current accounts.yml). Re-running is
safe and idempotent, with one asymmetry by design: existing users are left
alone (password only used for NEW users); existing Account rows have their
credentials/env/label/broker_host refreshed from accounts.yml every run
(yml stays the source of truth for credentials); existing AccountStrategy
LINKS are left completely untouched on re-run -- enabled/initial_balance/
preset/etc. are operator-tuned live via the webapp once a link exists, and
a stale accounts.yml must never be able to silently revert that (2026-08-06/
07 incident: this script used to clobber enabled/initial_balance on every
run, see `_upsert_account_strategy`). Nothing here is ever duplicated.

Account identity: cTrader rows are keyed by ACCOUNT_ID (ctidTraderAccountId).
Bybit has no such id in the yml, so a BYBIT row is keyed by its `name:` label
instead, which is also what bot/accounts_config.py uses to tell two
sub-accounts of the same person apart. This matters: keying Bybit on
(user_id, broker, external_account_id=None) alone -- as this script did until
2026-08-06 -- makes every BYBIT row of one user collide, so a second entry
does not create a second account, it silently OVERWRITES the first one's
credentials. A BYBIT entry without `name:` still falls back to
`bybit-<username>`, which is safe only while that user has exactly one Bybit
account.

Usage:
    python -m scripts.migrate_accounts_yml --password '<plaintext>'
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from webapp.db import init_db, get_session  # noqa: E402
from webapp.models import Account, AccountStrategy, Strategy, User  # noqa: E402
from webapp.schemas import (  # noqa: E402
    AccountCreate, AccountStrategyCreate, Broker, Env, StrategyCreate,
    Strategy as StrategyEnum,
)
from webapp.security import hash_password  # noqa: E402

ACCOUNTS_YML = ROOT / "configs" / "accounts.yml"

STRATEGY_DEFAULTS = {
    "S007": dict(broker=Broker.CTRADER, default_preset="WORKING_S007"),
    "S009": dict(broker=Broker.BYBIT, default_preset=None),
}


def _get_or_create_user(s, username: str, password: str) -> User:
    u = s.query(User).filter_by(username=username).one_or_none()
    if u:
        return u
    u = User(username=username, password_hash=hash_password(password))
    s.add(u)
    s.flush()
    print(f"  created user {username!r} (id={u.id})")
    return u


def _get_or_create_strategy(s, name: str) -> Strategy:
    st = s.query(Strategy).filter_by(name=name).one_or_none()
    if st:
        return st
    defaults = STRATEGY_DEFAULTS[name]
    payload = StrategyCreate(name=StrategyEnum(name), broker=defaults["broker"],
                              default_preset=defaults["default_preset"])
    st = Strategy(name=payload.name.value, broker=payload.broker.value,
                  default_preset=payload.default_preset)
    s.add(st)
    s.flush()
    print(f"  created strategy {name!r} (id={st.id})")
    return st


def _upsert_account(s, payload: AccountCreate) -> Account:
    # Identity: the broker's own account id when there is one (cTrader), the
    # label otherwise (Bybit -- see the module docstring). Matching on a NULL
    # external_account_id would make every Bybit row of one user the same row.
    q = s.query(Account).filter_by(user_id=payload.user_id, broker=payload.broker.value)
    if payload.external_account_id:
        q = q.filter_by(external_account_id=payload.external_account_id)
    else:
        q = q.filter_by(external_account_id=None, label=payload.label)
    existing = q.one_or_none()
    if existing:
        existing.env = payload.env.value
        existing.label = payload.label
        existing.broker_host = payload.broker_host
        existing.credentials = payload.credentials
        print(f"  updated account {payload.broker.value} "
              f"{payload.external_account_id or payload.label} (id={existing.id})")
        return existing
    acc = Account(user_id=payload.user_id, broker=payload.broker.value,
                  external_account_id=payload.external_account_id, env=payload.env.value,
                  label=payload.label, broker_host=payload.broker_host)
    acc.credentials = payload.credentials
    s.add(acc)
    s.flush()
    print(f"  created account {payload.broker.value} "
          f"{payload.external_account_id or payload.label} (id={acc.id})")
    return acc


def _upsert_account_strategy(s, payload: AccountStrategyCreate) -> AccountStrategy:
    """Create the link on first import; leave an existing link's tunables
    alone on every re-run.

    2026-08-06/07 incident: this used to overwrite enabled/preset/symbol/
    risk_pct/fixed_lot/use_fixed_lot/initial_balance on EVERY run, including
    for a link that already existed. enabled and initial_balance are
    operator-tuned live via the webapp (Anton manually flipped
    Bybit-tradebot1/Bybit-algo009's enabled state and corrected algo009's
    initial_balance 50->100 straight in the DB on 2026-08-06) -- a stale
    accounts.yml (still active: true / initial_balance: 50 for algo009) that
    happened to get re-run would have silently reverted both. Per the module
    docstring, the DB is the source of truth once a link exists; accounts.yml
    only seeds it once. If accounts.yml's link settings genuinely need to
    change, edit the DB (or the webapp UI) directly, not by re-running this
    script.
    """
    link = s.query(AccountStrategy).filter_by(
        account_id=payload.account_id, strategy_id=payload.strategy_id).one_or_none()
    if link is not None:
        print(f"  account_strategy link (id={link.id}) already exists -- left untouched "
              f"(enabled/preset/initial_balance/etc. are operator-tuned, not re-imported)")
        return link
    link = AccountStrategy(
        account_id=payload.account_id, strategy_id=payload.strategy_id,
        enabled=payload.enabled, preset=payload.preset, symbol=payload.symbol,
        risk_pct=payload.risk_pct, fixed_lot=payload.fixed_lot,
        use_fixed_lot=payload.use_fixed_lot, initial_balance=payload.initial_balance,
    )
    s.add(link)
    s.flush()
    print(f"  created account_strategy link (id={link.id})")
    return link


def migrate(password: str):
    init_db()
    s = get_session()
    data = yaml.safe_load(ACCOUNTS_YML.read_text(encoding="utf-8")) or {}

    for row in data.get("CTRADER", []):
        print(f"CTRADER {row.get('ACCOUNT_ID')} ({row['username']}):")
        user = _get_or_create_user(s, row["username"], password)
        strat = _get_or_create_strategy(s, "S007")
        env = row.get("env")
        if not env:
            raise ValueError(
                f"CTRADER entry for {row['username']!r} (ACCOUNT_ID={row.get('ACCOUNT_ID')}) "
                f"has no `env:` field -- add demo/live to configs/accounts.yml before migrating")
        acc_payload = AccountCreate(
            user_id=user.id, broker=Broker.CTRADER,
            external_account_id=str(row["ACCOUNT_ID"]), env=Env(env),
            label=f"ctrader-{row['ACCOUNT_ID']}",
            credentials=dict(client_id=row["CLIENT_ID"], client_secret=row["CLIENT_SECRET"],
                              access_token=row["ACCESS_TOKEN"]))
        acc = _upsert_account(s, acc_payload)
        link_payload = AccountStrategyCreate(
            account_id=acc.id, strategy_id=strat.id, enabled=bool(row.get("active", True)),
            initial_balance=row.get("initial_balance"))
        _upsert_account_strategy(s, link_payload)

    for row in data.get("BYBIT", []):
        print(f"BYBIT {row.get('name') or ''} ({row['username']}):")
        user = _get_or_create_user(s, row["username"], password)
        strat = _get_or_create_strategy(s, "S009")
        env = row.get("env") or ("testnet" if row.get("TESTNET") else "mainnet")
        acc_payload = AccountCreate(
            user_id=user.id, broker=Broker.BYBIT, external_account_id=None, env=Env(env),
            label=row.get("name") or f"bybit-{row['username']}",
            credentials=dict(api_key=row["API_KEY"], api_secret=row["API_SECRET"]))
        acc = _upsert_account(s, acc_payload)
        link_payload = AccountStrategyCreate(
            account_id=acc.id, strategy_id=strat.id, enabled=bool(row.get("active", True)),
            initial_balance=row.get("initial_balance"))
        _upsert_account_strategy(s, link_payload)

    s.commit()
    print("migration complete")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--password", required=True, help="password for newly created users")
    a = ap.parse_args()
    migrate(a.password)
