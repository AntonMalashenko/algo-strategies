"""cTrader credentials for DB-registered accounts, with the OAuth2 access
token kept fresh centrally (ALGODEV-48).

Why a central, locked refresh on top of the per-session pre-flight
------------------------------------------------------------------
Both cTrader bases already refresh pre-flight (`bot/ctrader.py`,
`bot/clients/ctrader/client.py`, one decision in
`bot.clients.ctrader.auth.refresh_if_needed`). That alone is not enough for
the DB-driven runner, because several PROCESSES can open a session on the
SAME account in the same minute: S007 and S021 share one cTrader account
(IC Markets and FTMO alike), each strategy worker is its own subprocess, and
the end-of-cycle position sync and the pre-session position audit are more
subprocesses on that account. When the token crosses its refresh threshold,
all of them would refresh at once with the same refresh token. cTrader may
rotate the refresh token on every renewal, so the loser of that race either
fails outright or -- worse -- overwrites the stored pair with one the broker
has already invalidated, and every later cycle is back in the
CH_ACCESS_TOKEN_INVALID outage (S011, 2026-09-21..22).

So every DB-driven entry point asks `fresh_ctrader_creds()` for its creds.
It refreshes under a per-account exclusive file lock and RE-READS the
account inside the lock, so the first process renews and persists and the
others simply pick up the already-renewed pair.

It refreshes with a WIDER leeway than the per-session pre-flight
(`CENTRAL_REFRESH_LEEWAY_S` vs `auth.REFRESH_LEEWAY_S`): whenever this
function decides "still valid", the adapter's own pre-flight moments later
is then guaranteed to agree, so an unlocked renewal inside the cycle cannot
happen on the normal path. The pre-flight stays as a backstop (and still
gets a persister) for the case where the central refresh failed.

Failure policy: a refresh that fails here is logged and the stale creds are
returned instead of raising. The token may well still be valid (we refresh
up to `CENTRAL_REFRESH_LEEWAY_S` early), and if it is not, the cycle fails
with the broker's own error, which lands in `AccountStrategy.last_error`
where scripts/podman_healthcheck.py alerts on it immediately
(`auth.needs_human_reauth`).

The lock is `fcntl.flock` on a file under data/: every trading process runs
in the same Podman VM (one kernel) with data/ bind-mounted, and a flock is
released by the kernel when its holder dies -- no stale-lock cleanup needed,
unlike an O_EXCL marker file.
"""
from __future__ import annotations

import fcntl
import time
from contextlib import contextmanager
from pathlib import Path

from bot.clients.ctrader import auth

ROOT = Path(__file__).resolve().parent.parent

# Per-account lock files live next to the DB, on the bind-mounted volume every
# worker container shares (docker-compose.yml's ${PWD}/data mount).
TOKEN_LOCK_DIR = ROOT / "data" / ".token_locks"

# Twice the per-session pre-flight leeway -- see the module docstring for why
# it must be strictly wider than auth.REFRESH_LEEWAY_S.
CENTRAL_REFRESH_LEEWAY_S = 2 * auth.REFRESH_LEEWAY_S

# How long to wait for another process's refresh before giving up and going
# ahead with the stored creds. A refresh is one HTTP GET bounded by
# auth.HTTP_TIMEOUT_S, so the holder is done well inside this; waiting longer
# would eat into a worker's ~55s tick budget (webapp/runner.py).
LOCK_WAIT_S = 2 * auth.HTTP_TIMEOUT_S
LOCK_POLL_S = 0.2


def ctrader_creds(acc) -> dict:
    """The cTrader credential dict every DB-driven path hands to the broker
    layer -- one implementation, so a new credential field reaches every
    strategy, the position sync, the audit and the daemon at once."""
    creds_row = acc.credentials
    return dict(
        client_id=creds_row.get("client_id"),
        client_secret=creds_row.get("client_secret"),
        access_token=creds_row.get("access_token"),
        refresh_token=creds_row.get("refresh_token"),
        token_expires_at=creds_row.get("token_expires_at"),
        account_id=int(acc.external_account_id) if acc.external_account_id else None,
        host=acc.broker_host)


def token_persister(session, acc):
    """Callback the cTrader broker layer invokes after renewing a token.

    Merges the renewed fields into the account's encrypted credentials and
    commits IMMEDIATELY: a later crash in the same cycle must not lose the
    new token, and after a refresh-token rotation the old pair is dead.
    """
    def persist(refreshed: dict) -> None:
        acc.credentials = {**acc.credentials, **refreshed}
        session.commit()
        print(f"[ctrader-tokens] account {acc.id}: cTrader access token refreshed, "
              f"valid until {refreshed.get('token_expires_at')}")
    return persist


@contextmanager
def _account_lock(account_id: int, *, wait_s: float = LOCK_WAIT_S):
    """Exclusive per-account flock. Yields True when held, False when the
    wait ran out (the caller then proceeds without refreshing)."""
    TOKEN_LOCK_DIR.mkdir(parents=True, exist_ok=True)
    with open(TOKEN_LOCK_DIR / f"account-{account_id}.lock", "a+") as handle:
        deadline = time.monotonic() + wait_s
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    yield False
                    return
                time.sleep(LOCK_POLL_S)
        try:
            yield True
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _due(creds: dict, leeway_s: int) -> bool:
    """Cheap, lock-free check: is a refresh both needed and possible?"""
    bundle = auth.TokenBundle.from_credentials(creds)
    return (bundle is not None and bool(bundle.refresh_token)
            and bundle.needs_refresh(leeway_s=leeway_s))


def fresh_ctrader_creds(session, acc, *,
                        leeway_s: int = CENTRAL_REFRESH_LEEWAY_S) -> dict:
    """cTrader creds for `acc` whose access token stays valid for at least
    `leeway_s`, refreshing (under the per-account lock) and persisting first
    when needed. Never raises on a refresh failure -- see the module
    docstring. `session` must be the session `acc` is attached to; it is
    committed when a refresh is persisted.
    """
    creds = ctrader_creds(acc)
    if not _due(creds, leeway_s):
        return creds

    with _account_lock(acc.id) as locked:
        if not locked:
            print(f"[ctrader-tokens] account {acc.id}: token refresh lock busy for "
                  f"{LOCK_WAIT_S}s -- proceeding with the stored token")
            return creds
        # Re-read inside the lock: the process that held it before us has
        # very likely just renewed (and possibly rotated) this very token.
        session.refresh(acc)
        creds = ctrader_creds(acc)
        try:
            refreshed = auth.refresh_if_needed(creds, leeway_s=leeway_s)
        except auth.TokenRefreshError as exc:
            print(f"[ctrader-tokens] account {acc.id}: token refresh FAILED -- "
                  f"proceeding with the stored token: {exc}")
            return creds
        if refreshed is not None:
            token_persister(session, acc)(refreshed.as_credentials())
            creds.update(refreshed.as_credentials())
    return creds
