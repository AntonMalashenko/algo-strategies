"""Admin CLI for the bot DB (until the web UI is wired):

    python -m webapp.cli init-db
    python -m webapp.cli create-user --username anton --password *** [--admin] \\
        [--client-id ... --client-secret ... --access-token ...]
    python -m webapp.cli set-creds --username anton --access-token ... [--client-id ...]
    python -m webapp.cli add-account --username anton --ctid 123456 --label "demo1" \\
        [--preset BASELINE_S007 --risk 0.25 --lot 0.01 --enable]
    python -m webapp.cli enable  --account-id 1 / disable --account-id 1
    python -m webapp.cli list
"""
from __future__ import annotations

import argparse

from webapp.db import get_session, init_db
from webapp.models import User, Account
from webapp.security import hash_password


def _user(session, username):
    u = session.query(User).filter_by(username=username).one_or_none()
    if not u:
        raise SystemExit(f"user '{username}' not found")
    return u


def cmd_init_db(a):
    init_db()
    print("db initialised")


def cmd_create_user(a):
    init_db()
    s = get_session()
    if s.query(User).filter_by(username=a.username).first():
        raise SystemExit("username already exists")
    u = User(username=a.username, password_hash=hash_password(a.password), is_admin=a.admin)
    if a.client_id:
        u.ctrader_client_id = a.client_id
    if a.client_secret:
        u.client_secret = a.client_secret
    if a.access_token:
        u.access_token = a.access_token
    s.add(u); s.commit()
    print(f"user '{a.username}' created (id={u.id})")


def cmd_set_creds(a):
    s = get_session(); u = _user(s, a.username)
    if a.client_id is not None:
        u.ctrader_client_id = a.client_id
    if a.client_secret is not None:
        u.client_secret = a.client_secret
    if a.access_token is not None:
        u.access_token = a.access_token
    s.commit(); print("credentials updated (encrypted at rest)")


def cmd_add_account(a):
    s = get_session(); u = _user(s, a.username)
    acc = Account(user_id=u.id, ctid_trader_account_id=a.ctid, label=a.label or "",
                  preset=a.preset, risk_pct=a.risk, fixed_lot=a.lot, enabled=a.enable,
                  is_live=a.live)
    s.add(acc); s.commit()
    print(f"account {a.ctid} added for '{a.username}' (id={acc.id}, enabled={a.enable})")


def cmd_enable(a, on=True):
    s = get_session()
    acc = s.get(Account, a.account_id)
    if not acc:
        raise SystemExit("account not found")
    acc.enabled = on; s.commit()
    print(f"account {acc.id} enabled={on}")


def cmd_list(a):
    s = get_session()
    for u in s.query(User).all():
        tok = "set" if u._access_token_enc else "—"
        print(f"user {u.id} {u.username} admin={u.is_admin} token={tok}")
        for acc in u.accounts:
            print(f"   acct {acc.id} ctid={acc.ctid_trader_account_id} '{acc.label}' "
                  f"enabled={acc.enabled} preset={acc.preset} risk={acc.risk_pct} "
                  f"status={acc.status} last={acc.last_cycle_at}")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init-db")
    p = sub.add_parser("create-user")
    p.add_argument("--username", required=True); p.add_argument("--password", required=True)
    p.add_argument("--admin", action="store_true")
    p.add_argument("--client-id"); p.add_argument("--client-secret"); p.add_argument("--access-token")
    p = sub.add_parser("set-creds")
    p.add_argument("--username", required=True)
    p.add_argument("--client-id"); p.add_argument("--client-secret"); p.add_argument("--access-token")
    p = sub.add_parser("add-account")
    p.add_argument("--username", required=True); p.add_argument("--ctid", type=int, required=True)
    p.add_argument("--label", default=""); p.add_argument("--preset", default="BASELINE_S007")
    p.add_argument("--risk", type=float, default=0.25); p.add_argument("--lot", type=float, default=0.01)
    p.add_argument("--enable", action="store_true"); p.add_argument("--live", action="store_true")
    p = sub.add_parser("enable"); p.add_argument("--account-id", type=int, required=True)
    p = sub.add_parser("disable"); p.add_argument("--account-id", type=int, required=True)
    sub.add_parser("list")
    a = ap.parse_args()
    {"init-db": cmd_init_db, "create-user": cmd_create_user, "set-creds": cmd_set_creds,
     "add-account": cmd_add_account, "list": cmd_list,
     "enable": lambda a: cmd_enable(a, True), "disable": lambda a: cmd_enable(a, False),
     }[a.cmd](a)


if __name__ == "__main__":
    main()
