"""scripts/seed_ftmo_account.py -- one-off, idempotent DB seeding for the
FTMO cTrader account S007 + S021 run on (ALGODEV-55, 2026-09-29).

Every value below was read live from the broker or from the FTMO client
area on 2026-09-29, not guessed:
  - ProtoOATraderReq via the fresh OAuth grant: ctid 48960621, traderLogin
    17211983, brokerName "ftmo", balance 10000.00, leverage 1:100, on the
    cTrader LIVE server (FTMO runs its evaluations there even though the
    money is not real) -- hence env="live" + the live host below.
  - ProtoOASymbolsListReq on that account: DAX = "GER40.cash" (symbolId 279),
    Nasdaq 100 = "US100.cash" (symbolId 275), min/step volume 0.01 lot.
  - FTMO client area "Objectives" (screenshot from Anton): max daily loss
    -$500, max loss -$1000, profit target +$500, min 2 trading days.
  - FTMO rules page (https://ftmo.com/en/trading-objectives/): daily loss is
    reset at 00:00 CE(S)T and counts equity; max loss is static.

Risk settings agreed with Anton in chat 2026-09-29 ("всё норм"): S007 0.25%
per trade / 2% daily budget, S021 0.50% per trade / 1% daily budget,
account guard -4.5% daily / -8% total (revised from -4% the same day: the
per-link daily budgets are the primary limiter, the account guard is only a
backstop for slippage/fees and the multi-day max loss) (FTMO's own limits are -5% / -10%),
both links start in broker_mode="dry" (logged, no real orders) and are
switched to "execute" by hand after the first day's check:

    python -m webapp.cli set-strategy-risk --account-strategy-id N --broker-mode execute

Credentials: client_id/client_secret are the same Open API application as
every other cTrader account (copied from the reference account), the access
+ refresh token come from data/pending_ctrader_token.enc -- a Fernet blob
(same APP_SECRET_KEY as the DB) written during the 2026-09-29 OAuth grant.
That file is deleted after a successful commit, so the token then lives
only in the DB, encrypted, like every other account's.

Usage (from the repo root, with APP_SECRET_KEY exported, after
`alembic upgrade head` has applied migration 008):
    python -m scripts.seed_ftmo_account --dry-run   # show what would change
    python -m scripts.seed_ftmo_account             # apply (backs up the DB first)
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from webapp.crypto import decrypt_secret  # noqa: E402
from webapp.db import get_session  # noqa: E402
from webapp.models import (Account, AccountStrategy, Asset, Broker,  # noqa: E402
                           BrokerAssetSymbol, Strategy, User)

PENDING_TOKEN_FILE = ROOT / "data" / "pending_ctrader_token.enc"
DB_FILE = ROOT / "data" / "app.db"

USERNAME = "anton.mal.slb@gmail.com"
REFERENCE_ACCOUNT_ID = 1          # ctrader-47939312: source of client_id/secret only

BROKER = dict(
    name="FTMO", is_prop_firm=True, platforms="CTRADER", algo_allowed=True,
    daily_loss_cap_pct=5.0, max_drawdown_pct=10.0, profit_split_pct=None,
    evaluation_type="2-step",
    policy_source="https://ftmo.com/en/trading-objectives/ (read 2026-09-29) + FTMO client area",
)

CTRADER_LIVE_HOST = "live.ctraderapi.com"   # ctrader_open_api EndPoints.PROTOBUF_LIVE_HOST
ACCOUNT = dict(
    broker="CTRADER", env="live", external_account_id="48960621",
    broker_account_number="17211983", label="ftmo-17211983",
    broker_host=CTRADER_LIVE_HOST, initial_balance=10000.0,
    guard_daily_loss_pct=4.5, guard_max_loss_pct=8.0, day_reset_tz="Europe/Prague",
    evaluation_phase="free_trial_2step", profit_target_pct=5.0,
)

LINKS = {
    "S007": dict(preset="WORKING_S007_NEWSSAFE_MAX8_BE05_OFF2", risk_pct=0.25,
                 daily_risk_cap_pct=2.0, use_fixed_lot=False, fixed_lot=0.01,
                 initial_balance=10000.0, broker_mode="dry", enabled=True),
    "S021": dict(preset=None, risk_pct=0.50, daily_risk_cap_pct=1.0,
                 use_fixed_lot=False, fixed_lot=0.01,
                 initial_balance=10000.0, broker_mode="dry", enabled=True),
}

SYMBOLS = {   # Asset.symbol -> FTMO's cTrader ticker (live-verified, see docstring)
    "GER40": "GER40.cash",
    "NASDAQ": "US100.cash",
}
SYMBOL_NOTES = ("Live-verified 2026-09-29: read-only ProtoOASymbolsListReq on FTMO account "
                "ctid 48960621 (login 17211983) during ALGODEV-55 onboarding; the only "
                "DAX / Nasdaq-100 names in FTMO's symbol list.")


def _upsert(session, model, lookup: dict, values: dict, dry: bool) -> tuple[object, str]:
    row = session.query(model).filter_by(**lookup).one_or_none()
    action = "update" if row is not None else "create"
    if row is None:
        row = model(**lookup)
        if not dry:
            session.add(row)
    diff = {k: v for k, v in values.items() if getattr(row, k, None) != v}
    for k, v in diff.items():
        setattr(row, k, v)
    print(f"  {action} {model.__tablename__} {lookup}: "
          f"{', '.join(f'{k}={v!r}' for k, v in diff.items()) or 'no change'}")
    return row, action


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    dry = args.dry_run

    session = get_session()
    if not hasattr(Account, "guard_daily_loss_pct"):
        raise SystemExit("models predate migration 008 -- pull the ALGODEV-55 code first")

    if not dry and DB_FILE.exists():
        backup = DB_FILE.with_name(
            f"app.db.bak-pre-ftmo-{datetime.now().strftime('%Y%m%d%H%M%S')}")
        shutil.copy2(DB_FILE, backup)
        print(f"backup: {backup}")

    user = session.query(User).filter_by(username=USERNAME).one()
    broker, _ = _upsert(session, Broker, dict(name=BROKER["name"]),
                        {**{k: v for k, v in BROKER.items() if k != "name"},
                         "policy_checked_at": datetime.now(timezone.utc).replace(tzinfo=None)},
                        dry)
    if not dry:
        session.flush()

    acc, action = _upsert(
        session, Account,
        dict(user_id=user.id, broker=ACCOUNT["broker"],
             external_account_id=ACCOUNT["external_account_id"]),
        {**{k: v for k, v in ACCOUNT.items() if k not in ("broker", "external_account_id")},
         "broker_id": broker.id}, dry)

    token = None
    if PENDING_TOKEN_FILE.exists():
        token = json.loads(decrypt_secret(PENDING_TOKEN_FILE.read_text()))
    elif action == "create":
        raise SystemExit(f"{PENDING_TOKEN_FILE} missing -- no token for the new account")
    if token is not None:
        ref = session.get(Account, REFERENCE_ACCOUNT_ID).credentials
        creds = dict(client_id=ref["client_id"], client_secret=ref["client_secret"], **token)
        print(f"  set credentials on {ACCOUNT['label']}: client app from account "
              f"{REFERENCE_ACCOUNT_ID}, token valid until {token.get('token_expires_at')}")
        if not dry:
            acc.credentials = creds
    if not dry:
        session.flush()

    for name, values in LINKS.items():
        strat = session.query(Strategy).filter_by(name=name).one()
        _upsert(session, AccountStrategy, dict(account_id=acc.id, strategy_id=strat.id),
                values, dry)

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    for asset_symbol, ticker in SYMBOLS.items():
        asset = session.query(Asset).filter_by(symbol=asset_symbol).one()
        _upsert(session, BrokerAssetSymbol,
                dict(broker_id=broker.id, asset_id=asset.id, platform="CTRADER"),
                dict(broker_symbol=ticker, verified_at=now, notes=SYMBOL_NOTES), dry)

    if dry:
        session.rollback()
        print("dry run -- nothing written")
        return
    session.commit()
    links = session.query(AccountStrategy).filter_by(account_id=acc.id).all()
    print(f"committed: account id={acc.id}; links: "
          + ", ".join(f"{l.strategy.name}=id {l.id} ({l.broker_mode})" for l in links))
    if token is not None:
        PENDING_TOKEN_FILE.unlink()
        print(f"removed {PENDING_TOKEN_FILE.name} (token now only in the DB, encrypted)")


if __name__ == "__main__":
    main()
