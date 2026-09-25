"""scripts/ctrader_oauth.py — one-time helper to mint a fresh cTrader Open
API access token via the OAuth2 authorization-code flow, for when the
stored token expires (see bot/ctrader.py's _auth_account()/_load_symbols()
error surfacing, added 2026-08-17 after CH_ACCESS_TOKEN_INVALID silently
stopped S007 all session).

Endpoints per the official docs (Open API portal -> Documentation ->
"App and account authentication", read live 2026-08-17 -- an earlier
version of this script guessed connect.spotware.com endpoints that don't
exist; id.ctrader.com/openapi.ctrader.com are the real ones):
  - authorize: https://id.ctrader.com/my/settings/openapi/grantingaccess/
  - token:     GET https://openapi.ctrader.com/apps/token (query params,
    not a JSON POST body)

The redirect_uri you use here must be registered on the app (Open API
portal -> Applications -> Edit -> Redirect URIs) and must NOT be the
first/Playground one -- the docs explicitly say that one "must never be
used for a production application". Add a second redirect URI first (any
placeholder like http://localhost/ works fine, nothing needs to actually
listen there -- you just copy the `code` out of the resulting URL bar).

Credentials (CLIENT_ID/CLIENT_SECRET) come from configs/accounts.yml's
CTRADER section, same source as bot/config.py::ctrader_credentials() --
this script does not take them as arguments so they're never typed on the
command line or logged in shell history.

Usage, two steps (cTrader's OAuth flow needs a human login in a browser --
this can't be done headlessly):

    # 1) Print the URL to open and log in with. --redirect-uri must be
    #    EXACTLY one of the (non-Playground) URIs registered for this app.
    python -m scripts.ctrader_oauth auth-url --redirect-uri http://localhost/

    # 2) After approving, the browser is redirected to
    #    <redirect-uri>?code=... -- the page can 404 (e.g. for
    #    http://localhost/ with nothing listening), the code is still in
    #    the address bar's query string regardless. Paste it here:
    python -m scripts.ctrader_oauth exchange --code PASTE_CODE_HERE \
        --redirect-uri http://localhost/

    # exchange prints the new tokens. --write updates
    # configs/accounts.yml's CTRADER.ACCESS_TOKEN in place (that file is
    # gitignored -- see .gitignore:31 -- never committed); --db-account
    # updates the encrypted credentials of one webapp Account, which is what
    # the live runner actually reads.

Storing the refresh token is what lets the bot renew its own access token
from then on (bot/clients/ctrader/auth.py), so the CH_ACCESS_TOKEN_INVALID
outage that cost S011 2026-09-21..22 cannot repeat. Prefer --db-account for
any account the runner drives.

The OAuth2 protocol itself lives in bot/clients/ctrader/auth.py -- this
script is only the human-in-the-loop front end for it, so there is exactly
one implementation of the token endpoints.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from bot import config as C  # noqa: E402
from bot.clients.ctrader import auth  # noqa: E402

ACCOUNTS_YML = ROOT / "configs" / "accounts.yml"


def _creds() -> dict:
    creds = C.ctrader_credentials()
    if not creds.get("client_id") or not creds.get("client_secret"):
        raise SystemExit("CTRADER client_id/client_secret not found via "
                          "bot.config.ctrader_credentials() -- check configs/accounts.yml")
    return creds


def cmd_auth_url(args: argparse.Namespace) -> None:
    creds = _creds()
    print(auth.authorization_url(creds["client_id"], args.redirect_uri))
    print("\nOpen this URL, log in, approve -- then copy the `code` query "
          "param from the redirect URL (the page itself can 404, that's fine). "
          "The authorization code expires in 1 minute -- run `exchange` right away.")


def _write_accounts_yml(old_token: str, new_token: str) -> None:
    """Replace the ACCESS_TOKEN value in configs/accounts.yml in place.

    Only the access token: accounts.yml is the single-account CLI path, and
    bot/config.py reads REFRESH_TOKEN/TOKEN_EXPIRES_AT from it only if they
    are already present, so adding them is a manual edit by design (we do
    not want this script inventing YAML structure in a hand-maintained file).
    """
    text = ACCOUNTS_YML.read_text()
    pattern = re.compile(r"(ACCESS_TOKEN:\s*)" + re.escape(old_token))
    new_text, replacements = pattern.subn(r"\g<1>" + new_token, text, count=1)
    if replacements != 1:
        print(f"\nCould not find the old ACCESS_TOKEN value in {ACCOUNTS_YML} to replace "
              f"(expected exactly 1 match, found {replacements}) -- update it manually.",
              file=sys.stderr)
        raise SystemExit(1)
    ACCOUNTS_YML.write_text(new_text)
    print(f"\nWrote new access_token into {ACCOUNTS_YML}")


def _write_db_account(account_id: int, bundle: auth.TokenBundle) -> None:
    """Store the whole bundle in one webapp Account's encrypted credentials.

    This is the path that matters for a scheduled bot: the runner reads
    credentials from the DB, and persisting the refresh token here is what
    enables unattended renewal from the next cycle onward.
    """
    from webapp.db import get_session
    from webapp.models import Account

    session = get_session()
    try:
        account = session.get(Account, account_id)
        if account is None:
            raise SystemExit(f"no Account with id={account_id} in the database")
        if account.broker != "CTRADER":
            raise SystemExit(f"Account id={account_id} is broker={account.broker}, not CTRADER")
        account.credentials = {**account.credentials, **bundle.as_credentials()}
        session.commit()
        print(f"\nStored access + refresh token for Account id={account_id} "
              f"({account.label or account.external_account_id}), "
              f"valid until {bundle.expires_at.isoformat()}")
    finally:
        session.close()


def cmd_exchange(args: argparse.Namespace) -> None:
    creds = _creds()
    try:
        bundle = auth.exchange_authorization_code(
            creds["client_id"], creds["client_secret"],
            code=args.code, redirect_uri=args.redirect_uri)
    except auth.TokenRefreshError as error:
        print(f"Token exchange failed: {error}", file=sys.stderr)
        raise SystemExit(1)

    print(f"access_token:  {bundle.access_token}")
    print(f"refresh_token: {bundle.refresh_token}")
    print(f"expires_at:    {bundle.expires_at.isoformat()}")

    if args.db_account is not None:
        _write_db_account(args.db_account, bundle)
    if args.write:
        _write_accounts_yml(str(creds["access_token"]), bundle.access_token)
    if args.db_account is None and not args.write:
        print("\n(neither --db-account nor --write passed -- nothing was stored; "
              "the refresh token above is what enables auto-renewal, so store it)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    p1 = sub.add_parser("auth-url")
    p1.add_argument("--redirect-uri", required=True)
    p1.set_defaults(func=cmd_auth_url)

    p2 = sub.add_parser("exchange")
    p2.add_argument("--code", required=True)
    p2.add_argument("--redirect-uri", required=True)
    p2.add_argument("--write", action="store_true",
                     help="overwrite configs/accounts.yml's ACCESS_TOKEN in place")
    p2.add_argument("--db-account", type=int,
                     help="webapp Account id whose encrypted credentials to update "
                          "(the path the scheduled runner actually reads)")
    p2.set_defaults(func=cmd_exchange)

    args = ap.parse_args()
    args.func(args)
