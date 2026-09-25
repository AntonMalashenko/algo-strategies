"""cTrader Open API OAuth2 tokens — mint, refresh, and expiry bookkeeping.

Why this module exists
----------------------
A cTrader access token expires. When it does, every request in the session
is rejected with `CH_ACCESS_TOKEN_INVALID` and the bot silently stops
trading until a human re-mints the token by hand. That happened live:
S011 lost 2026-09-21 entirely plus 2026-09-22 up to 09:45 (130 consecutive
cycles, `account auth failed: CH_ACCESS_TOKEN_INVALID: Access token
expired`), and the ESTOXX50/DOW exit signalled on 2026-09-18 only reached
the broker on 2026-09-22 as a knock-on effect.

`scripts/ctrader_oauth.py` could already mint a token, but only through a
two-step browser login driven by a human, and it explicitly did not use the
`refreshToken` the broker hands back alongside the access token. This
module is the reusable half of that flow: the pure-HTTP token operations,
usable by any strategy, with no Twisted and no CLI in the way.

Why it is deliberately Twisted-free
-----------------------------------
A refresh MUST happen before the reactor starts. Two hard constraints make
every other placement impossible:

  * the token call is blocking HTTP, and blocking inside a Twisted callback
    stalls the reactor;
  * a Twisted reactor can only be run once per OS process (see
    `bot/ctrader_s011.py`'s module docstring), so a session that dies on an
    expired token cannot simply be retried in the same process.

So the model is PRE-FLIGHT, not retry-on-failure: the client refreshes
while the token is still valid (`REFRESH_LEEWAY_S` before real expiry) and
only then opens the broker session. `is_expired_token_error` remains useful
for turning a failed cycle into an actionable log line, and for forcing a
refresh on the next cycle when expiry bookkeeping is missing or wrong.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import requests

# ---------------------------------------------------------------------------
# Protocol constants (fixed by the cTrader Open API, not tunables)
# ---------------------------------------------------------------------------

# Official endpoints (Open API portal -> Documentation -> "App and account
# authentication"). NOT connect.spotware.com, which does not exist -- see
# scripts/ctrader_oauth.py's module docstring for that earlier wrong guess.
AUTHORIZE_URL = "https://id.ctrader.com/my/settings/openapi/grantingaccess/"
TOKEN_URL = "https://openapi.ctrader.com/apps/token"

GRANT_AUTHORIZATION_CODE = "authorization_code"  # first, human-driven mint
GRANT_REFRESH_TOKEN = "refresh_token"            # headless renewal

DEFAULT_SCOPE = "trading"   # the only scope the bots need: read + trade
DEFAULT_PRODUCT = "web"     # authorize-URL product type, per the docs

HTTP_TIMEOUT_S = 20         # token endpoint is a small JSON GET; fail fast

# The broker's own name for "this access token is no longer usable". Matched
# as a substring of the error text because it reaches us wrapped in a
# RuntimeError built by the client's account-auth check, not as a code.
EXPIRED_TOKEN_ERROR_CODE = "CH_ACCESS_TOKEN_INVALID"

# Refresh this long before the token's real expiry. A cTrader access token
# lives ~30 days, and the bots tick at most every minute, so an hour of
# slack costs nothing and removes any chance of a token dying mid-session
# (the session is opened once and then used for the whole cycle).
REFRESH_LEEWAY_S = 3600

# Fallback lifetime when the token endpoint answers without `expiresIn`.
# Deliberately short: an under-estimate only causes a harmless early
# refresh, while an over-estimate recreates the very outage this module
# exists to prevent.
FALLBACK_EXPIRES_IN_S = 3600


class TokenRefreshError(RuntimeError):
    """The token endpoint refused to mint or renew a token.

    Raised instead of a bare RuntimeError so a caller can distinguish "the
    credentials themselves are dead, a human must re-authorise" from any
    other transport failure, and alert accordingly.
    """


@dataclass(frozen=True)
class TokenBundle:
    """One cTrader token pair plus the moment the access token dies.

    `expires_at` is always timezone-aware UTC. It is computed here, at mint
    time, rather than stored as the broker's relative `expiresIn`, so the
    value stays meaningful after being persisted and re-read days later.
    """

    access_token: str
    refresh_token: str | None
    expires_at: datetime

    def needs_refresh(self, *, now: datetime | None = None,
                      leeway_s: int = REFRESH_LEEWAY_S) -> bool:
        """True when the access token is expired, or close enough that it
        could die mid-session and should be renewed pre-flight."""
        now = now or datetime.now(timezone.utc)
        return now >= self.expires_at - timedelta(seconds=leeway_s)

    def as_credentials(self) -> dict:
        """The persistable shape: exactly the keys a credentials dict
        carries, so a caller can `creds.update(bundle.as_credentials())`
        without knowing this class's field names."""
        return {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "token_expires_at": self.expires_at.isoformat(),
        }

    @classmethod
    def from_credentials(cls, creds: dict) -> "TokenBundle | None":
        """Rebuild a bundle from a stored credentials dict.

        Returns None when there is no access token at all. An absent or
        unparseable `token_expires_at` is treated as "expires right now",
        which makes the client refresh on its next open instead of trusting
        a token whose lifetime nobody recorded -- the safe direction, since
        a needless refresh is cheap and a wrong "still valid" is an outage.
        """
        access_token = creds.get("access_token")
        if not access_token:
            return None
        return cls(access_token=access_token,
                   refresh_token=creds.get("refresh_token"),
                   expires_at=_parse_expires_at(creds.get("token_expires_at")))


def _parse_expires_at(raw) -> datetime:
    """Stored ISO timestamp -> aware UTC datetime; unusable input -> now."""
    now = datetime.now(timezone.utc)
    if not raw:
        return now
    try:
        parsed = datetime.fromisoformat(str(raw))
    except ValueError:
        return now
    # A naive stored value is read as UTC: every writer here emits UTC, and
    # guessing local time would silently shift the expiry by hours.
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def is_expired_token_error(error: BaseException | str) -> bool:
    """True when `error` is the broker rejecting an expired access token.

    Accepts an exception or an already-stringified failure so it works both
    around a live call and over a logged error string.
    """
    return EXPIRED_TOKEN_ERROR_CODE in str(error)


def authorization_url(client_id: str, redirect_uri: str, *,
                      scope: str = DEFAULT_SCOPE,
                      product: str = DEFAULT_PRODUCT) -> str:
    """Browser URL for the one-time, human-driven authorization step.

    `redirect_uri` must be registered on the app and must not be the
    Playground one (the docs forbid it for production apps).
    """
    from urllib.parse import urlencode

    query = urlencode({"client_id": client_id, "redirect_uri": redirect_uri,
                       "scope": scope, "product": product})
    return f"{AUTHORIZE_URL}?{query}"


def exchange_authorization_code(client_id: str, client_secret: str, *,
                                code: str, redirect_uri: str) -> TokenBundle:
    """Trade a freshly approved authorization `code` for a token pair.

    This is the step a human still has to trigger: it is how an account
    gets its FIRST refresh token. Everything afterwards is headless.
    """
    return _request_token({
        "grant_type": GRANT_AUTHORIZATION_CODE,
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": client_id,
        "client_secret": client_secret,
    })


def refresh_access_token(client_id: str, client_secret: str,
                         refresh_token: str) -> TokenBundle:
    """Renew an access token headlessly, no browser and no human.

    The broker may rotate the refresh token as part of the answer, so the
    returned bundle's `refresh_token` must be persisted too -- reusing the
    old one after a rotation fails, which would put the account straight
    back into the outage this module prevents. When the answer carries no
    refresh token, the one passed in is kept.
    """
    if not refresh_token:
        raise TokenRefreshError(
            "no refresh_token stored for this account -- re-authorise once via "
            "`python -m scripts.ctrader_oauth auth-url ...` to obtain one")
    bundle = _request_token({
        "grant_type": GRANT_REFRESH_TOKEN,
        "refresh_token": refresh_token,
        "client_id": client_id,
        "client_secret": client_secret,
    })
    if bundle.refresh_token:
        return bundle
    return TokenBundle(access_token=bundle.access_token,
                       refresh_token=refresh_token,
                       expires_at=bundle.expires_at)


def _request_token(params: dict) -> TokenBundle:
    """Call the token endpoint and parse its answer into a TokenBundle.

    The endpoint is a GET with query params (not a JSON POST) and signals
    failure in TWO ways: a non-200 status, or a 200 carrying `errorCode`.
    Both are treated as failures; the secrets in `params` are never logged
    or echoed into the raised message.
    """
    response = requests.get(TOKEN_URL, params=params,
                            headers={"Accept": "application/json"},
                            timeout=HTTP_TIMEOUT_S)
    try:
        body = response.json()
    except ValueError:
        raise TokenRefreshError(
            f"cTrader token endpoint returned non-JSON (HTTP {response.status_code})")

    if response.status_code != 200 or body.get("errorCode") or "accessToken" not in body:
        raise TokenRefreshError(
            f"cTrader token request failed (HTTP {response.status_code}, "
            f"grant_type={params.get('grant_type')}): "
            f"{body.get('errorCode') or body.get('description') or body}")

    expires_in = int(body.get("expiresIn") or FALLBACK_EXPIRES_IN_S)
    return TokenBundle(
        access_token=body["accessToken"],
        refresh_token=body.get("refreshToken"),
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=expires_in),
    )
