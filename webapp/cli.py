"""Admin CLI for the bot DB (until the web UI exists), rewritten for the
broker-agnostic multi-strategy schema (webapp/models.py: User -> Account ->
AccountStrategy -> Strategy, credentials as one encrypted JSON blob per
Account). Goes through webapp/schemas/ for validation, same as any future
API request would -- this CLI is just another caller of that boundary.

    python -m webapp.cli init-db
    python -m webapp.cli create-user --username anton --password *** [--admin]

    # one row per broker credential set the user holds
    python -m webapp.cli add-account --username anton --broker CTRADER --env demo \\
        --external-account-id 47939312 --client-id ... --client-secret ... \\
        --access-token ... [--label demo1] [--broker-host demo.ctraderapi.com]
    python -m webapp.cli add-account --username anton --broker BYBIT --env testnet \\
        --api-key ... --api-secret ...

    # one row per strategy definition (seed once)
    python -m webapp.cli add-strategy --name S007 --broker CTRADER \\
        [--description "..."] [--default-preset BASELINE_S007]

    # link an account to a strategy it should run -- an account can run several
    python -m webapp.cli link-strategy --account-id 1 --strategy S007 \\
        [--preset ...] [--risk 0.25] [--lot 0.01] [--initial-balance 10000] [--enable]

    python -m webapp.cli enable  --account-strategy-id 1
    python -m webapp.cli disable --account-strategy-id 1
    python -m webapp.cli list
"""
from __future__ import annotations

import argparse

from pydantic import ValidationError

from webapp.db import get_session, init_db
from webapp.models import Account, AccountStrategy, Strategy, User
from webapp.schemas import AccountCreate, StrategyCreate
from webapp.security import hash_password


def _user(session, username: str) -> User:
    u = session.query(User).filter_by(username=username).one_or_none()
    if not u:
        raise SystemExit(f"user '{username}' not found")
    return u


def _strategy_by_name(session, name: str) -> Strategy:
    st = session.query(Strategy).filter_by(name=name).one_or_none()
    if not st:
        raise SystemExit(f"strategy '{name}' not found -- add it first with add-strategy")
    return st


def _credentials_from_args(a) -> dict:
    """Broker-specific credential dict from CLI flags. Shape is not checked
    here -- AccountCreate below validates it against
    webapp.schemas.accounts.CREDENTIALS_BY_BROKER, so a missing/extra field
    for the chosen broker fails with one clear pydantic error, not a later
    KeyError deep in the runner."""
    if a.broker == "CTRADER":
        return dict(client_id=a.client_id, client_secret=a.client_secret,
                    access_token=a.access_token)
    if a.broker == "BYBIT":
        return dict(api_key=a.api_key, api_secret=a.api_secret)
    raise SystemExit(f"unknown broker {a.broker!r}")


def cmd_init_db(a):
    init_db()
    print("db initialised (tables created if missing). For a fresh production DB, "
          "prefer 'alembic upgrade head' instead so migration history (webapp/migrations/) "
          "stays the single source of truth -- init_db()'s create_all() is a dev/test "
          "convenience and does not know about Alembic's version table.")


def cmd_create_user(a):
    init_db()
    s = get_session()
    if s.query(User).filter_by(username=a.username).first():
        raise SystemExit("username already exists")
    u = User(username=a.username, password_hash=hash_password(a.password), is_admin=a.admin)
    s.add(u)
    s.commit()
    print(f"user '{a.username}' created (id={u.id})")


def cmd_add_account(a):
    s = get_session()
    u = _user(s, a.username)
    try:
        payload = AccountCreate(
            user_id=u.id, broker=a.broker, env=a.env,
            external_account_id=a.external_account_id, label=a.label or "",
            broker_host=a.broker_host, credentials=_credentials_from_args(a),
        )
    except ValidationError as e:
        raise SystemExit(f"invalid account: {e}")
    dup = s.query(Account).filter_by(
        user_id=u.id, broker=payload.broker.value,
        external_account_id=payload.external_account_id).first()
    if dup:
        raise SystemExit("an account with this user/broker/external_account_id already exists")
    acc = Account(user_id=payload.user_id, broker=payload.broker.value, env=payload.env.value,
                  external_account_id=payload.external_account_id, label=payload.label,
                  broker_host=payload.broker_host)
    acc.credentials = payload.credentials   # encrypted by the model property setter
    s.add(acc)
    s.commit()
    print(f"account added for '{a.username}': id={acc.id} broker={acc.broker} "
          f"env={acc.env} external_id={acc.external_account_id}")


def cmd_add_strategy(a):
    s = get_session()
    if s.query(Strategy).filter_by(name=a.name).first():
        raise SystemExit(f"strategy '{a.name}' already exists")
    try:
        payload = StrategyCreate(name=a.name, broker=a.broker, description=a.description,
                                 default_preset=a.default_preset)
    except ValidationError as e:
        raise SystemExit(f"invalid strategy: {e}")
    st = Strategy(name=payload.name.value, broker=payload.broker.value,
                  description=payload.description, default_preset=payload.default_preset)
    s.add(st)
    s.commit()
    print(f"strategy '{a.name}' added (id={st.id}, broker={st.broker})")


def cmd_link_strategy(a):
    s = get_session()
    acc = s.get(Account, a.account_id)
    if not acc:
        raise SystemExit(f"account {a.account_id} not found")
    st = _strategy_by_name(s, a.strategy)
    if st.broker != acc.broker:
        raise SystemExit(f"strategy '{st.name}' needs broker={st.broker}, "
                          f"but account {acc.id} is broker={acc.broker}")
    if s.query(AccountStrategy).filter_by(account_id=acc.id, strategy_id=st.id).first():
        raise SystemExit(f"account {acc.id} already runs strategy '{st.name}'")
    link = AccountStrategy(
        account_id=acc.id, strategy_id=st.id,
        preset=a.preset or st.default_preset, symbol=a.symbol,
        risk_pct=a.risk, fixed_lot=a.lot, use_fixed_lot=not a.risk_based,
        initial_balance=a.initial_balance, enabled=a.enable,
    )
    s.add(link)
    s.commit()
    print(f"account {acc.id} now runs '{st.name}' (link id={link.id}, enabled={a.enable})")


def cmd_enable(a, on: bool):
    s = get_session()
    link = s.get(AccountStrategy, a.account_strategy_id)
    if not link:
        raise SystemExit("account-strategy link not found")
    link.enabled = on
    if not on:
        link.status = "stopped"
    s.commit()
    print(f"account-strategy {link.id} ({link.strategy.name}) enabled={on}")


def cmd_list(a):
    s = get_session()
    for u in s.query(User).all():
        print(f"user {u.id} {u.username} admin={u.is_admin}")
        for acc in u.accounts:
            print(f"  account {acc.id} broker={acc.broker} env={acc.env} "
                  f"external_id={acc.external_account_id} '{acc.label}'")
            for link in acc.strategy_links:
                print(f"    -> {link.strategy.name} enabled={link.enabled} "
                      f"preset={link.preset} risk={link.risk_pct} lot={link.fixed_lot} "
                      f"status={link.status} last={link.last_cycle_at}")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init-db")

    p = sub.add_parser("create-user")
    p.add_argument("--username", required=True)
    p.add_argument("--password", required=True)
    p.add_argument("--admin", action="store_true")

    p = sub.add_parser("add-account")
    p.add_argument("--username", required=True)
    p.add_argument("--broker", required=True, choices=["CTRADER", "BYBIT"])
    p.add_argument("--env", required=True, choices=["demo", "live", "testnet", "mainnet"])
    p.add_argument("--external-account-id", dest="external_account_id", default=None)
    p.add_argument("--label", default="")
    p.add_argument("--broker-host", dest="broker_host", default=None)
    # CTRADER credential fields
    p.add_argument("--client-id", dest="client_id", default=None)
    p.add_argument("--client-secret", dest="client_secret", default=None)
    p.add_argument("--access-token", dest="access_token", default=None)
    # BYBIT credential fields
    p.add_argument("--api-key", dest="api_key", default=None)
    p.add_argument("--api-secret", dest="api_secret", default=None)

    p = sub.add_parser("add-strategy")
    p.add_argument("--name", required=True, choices=["S007", "S009"])
    p.add_argument("--broker", required=True, choices=["CTRADER", "BYBIT"])
    p.add_argument("--description", default=None)
    p.add_argument("--default-preset", dest="default_preset", default=None)

    p = sub.add_parser("link-strategy")
    p.add_argument("--account-id", dest="account_id", type=int, required=True)
    p.add_argument("--strategy", required=True)
    p.add_argument("--preset", default=None)
    p.add_argument("--symbol", default=None)
    p.add_argument("--risk", type=float, default=0.25)
    p.add_argument("--lot", type=float, default=0.01)
    p.add_argument("--risk-based", dest="risk_based", action="store_true",
                   help="size from --risk instead of the fixed --lot")
    p.add_argument("--initial-balance", dest="initial_balance", type=float, default=None)
    p.add_argument("--enable", action="store_true")

    p = sub.add_parser("enable")
    p.add_argument("--account-strategy-id", dest="account_strategy_id", type=int, required=True)
    p = sub.add_parser("disable")
    p.add_argument("--account-strategy-id", dest="account_strategy_id", type=int, required=True)

    sub.add_parser("list")

    a = ap.parse_args()
    {
        "init-db": cmd_init_db,
        "create-user": cmd_create_user,
        "add-account": cmd_add_account,
        "add-strategy": cmd_add_strategy,
        "link-strategy": cmd_link_strategy,
        "enable": lambda a: cmd_enable(a, True),
        "disable": lambda a: cmd_enable(a, False),
        "list": cmd_list,
    }[a.cmd](a)


if __name__ == "__main__":
    main()
