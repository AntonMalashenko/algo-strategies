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

    # exchange prints the new token(s) and asks before overwriting
    # configs/accounts.yml's CTRADER.ACCESS_TOKEN in place (that file is
    # gitignored -- see .gitignore:31 -- never committed).
"""
from __future__ import annotations

import argparse
import re
import sys
import urllib.parse
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from bot import config as C  # noqa: E402

AUTH_URL = "https://id.ctrader.com/my/settings/openapi/grantingaccess/"
TOKEN_URL = "https://openapi.ctrader.com/apps/token"
ACCOUNTS_YML = ROOT / "configs" / "accounts.yml"


def _creds() -> dict:
    creds = C.ctrader_credentials()
    if not creds.get("client_id") or not creds.get("client_secret"):
        raise SystemExit("CTRADER client_id/client_secret not found via "
                          "bot.config.ctrader_credentials() -- check configs/accounts.yml")
    return creds


def cmd_auth_url(args: argparse.Namespace) -> None:
    creds = _creds()
    qs = urllib.parse.urlencode({
        "client_id": creds["client_id"],
        "redirect_uri": args.redirect_uri,
        "scope": "trading",
        "product": "web",
    })
    print(f"{AUTH_URL}?{qs}")
    print("\nOpen this URL, log in, approve -- then copy the `code` query "
          "param from the redirect URL (the page itself can 404, that's fine). "
          "The authorization code expires in 1 minute -- run `exchange` right away.")


def cmd_exchange(args: argparse.Namespace) -> None:
    creds = _creds()
    resp = requests.get(TOKEN_URL, params={
        "grant_type": "authorization_code",
        "code": args.code,
        "redirect_uri": args.redirect_uri,
        "client_id": creds["client_id"],
        "client_secret": creds["client_secret"],
    }, headers={"Accept": "application/json"}, timeout=20)
    body = resp.json()
    if resp.status_code != 200 or "accessToken" not in body or body.get("errorCode"):
        print(f"Token exchange failed (HTTP {resp.status_code}): {body}", file=sys.stderr)
        raise SystemExit(1)

    access_token = body["accessToken"]
    refresh_token = body.get("refreshToken")
    print(f"access_token:  {access_token}")
    if refresh_token:
        print(f"refresh_token: {refresh_token}  (not currently used by bot/ctrader.py -- "
              f"no auto-refresh wired up, this is FYI/future use only)")

    if not args.write:
        print("\n(--write not passed -- configs/accounts.yml left untouched)")
        return

    text = ACCOUNTS_YML.read_text()
    old_token = str(creds["access_token"])
    pattern = re.compile(r"(ACCESS_TOKEN:\s*)" + re.escape(old_token))
    new_text, n = pattern.subn(r"\g<1>" + access_token, text, count=1)
    if n != 1:
        print(f"\nCould not find the old ACCESS_TOKEN value in {ACCOUNTS_YML} to replace "
              f"(expected exactly 1 match, found {n}) -- update it manually.", file=sys.stderr)
        raise SystemExit(1)
    ACCOUNTS_YML.write_text(new_text)
    print(f"\nWrote new access_token into {ACCOUNTS_YML}")


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
    p2.set_defaults(func=cmd_exchange)

    args = ap.parse_args()
    args.func(args)
