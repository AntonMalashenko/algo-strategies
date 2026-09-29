"""ALGODEV-48 for the LEGACY cTrader base (bot/ctrader.py, CTraderS007,
CTraderORB) -- the adapter S007 and S021 still trade through.

Until this change only S011 (on the new bot/clients/ctrader client) renewed
its access token; S007/S021 would have hit the same CH_ACCESS_TOKEN_INVALID
outage that stopped S011 for ~34h on 2026-09-21..22. These tests pin the
pre-flight contract the adapter now shares with the client:

  * the decision is auth.refresh_if_needed (one implementation for both
    bases), and it runs BEFORE the reactor -- a session that dies on an
    expired token cannot be retried in the same process;
  * a renewal is handed to `on_token_refreshed` so the (possibly rotated)
    refresh token is persisted;
  * a still-valid token, or an account with no refresh token, is left alone.

No network and no reactor: `auth.refresh_access_token` is stubbed and the
reactor is never reached.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

pytest.importorskip("ctrader_open_api")

from bot import ctrader as legacy                  # noqa: E402
from bot.clients.ctrader import auth               # noqa: E402
from bot.ctrader_orb import CTraderORB             # noqa: E402
from bot.ctrader_s007 import CTraderS007           # noqa: E402

FAR_FUTURE = (datetime.now(timezone.utc) + timedelta(days=20)).isoformat()
ALREADY_EXPIRED = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
RENEWED_EXPIRY = datetime.now(timezone.utc) + timedelta(days=30)


def _creds(**overrides) -> dict:
    creds = dict(client_id="cid", client_secret="sec", access_token="old-access",
                 refresh_token="old-refresh", token_expires_at=ALREADY_EXPIRED,
                 account_id=12345, host="demo.ctraderapi.com")
    creds.update(overrides)
    return creds


@pytest.fixture
def refresh_calls(monkeypatch):
    """Stub the token endpoint; records every refresh and rotates the
    refresh token, the way the broker may."""
    calls = []

    def fake_refresh(client_id, client_secret, refresh_token):
        calls.append((client_id, client_secret, refresh_token))
        return auth.TokenBundle(access_token="new-access", refresh_token="rotated-refresh",
                                expires_at=RENEWED_EXPIRY)
    monkeypatch.setattr(auth, "refresh_access_token", fake_refresh)
    return calls


class TestPreflightDecision:
    def test_expired_token_is_renewed_and_persisted(self, refresh_calls):
        persisted = []
        api = CTraderS007(creds=_creds(), on_token_refreshed=persisted.append)

        api._ensure_fresh_token()

        assert refresh_calls == [("cid", "sec", "old-refresh")]
        assert api.token == "new-access"            # what _auth_account will send
        assert api.refresh_token == "rotated-refresh"
        assert persisted == [{"access_token": "new-access",
                              "refresh_token": "rotated-refresh",
                              "token_expires_at": RENEWED_EXPIRY.isoformat()}]

    def test_unknown_expiry_counts_as_expired(self, refresh_calls):
        """Every account stored before this feature has no expiry recorded --
        the first cycle must refresh once and record one, not trust it."""
        api = CTraderS007(creds=_creds(token_expires_at=None))
        api._ensure_fresh_token()
        assert len(refresh_calls) == 1

    def test_valid_token_is_left_alone(self, refresh_calls):
        persisted = []
        api = CTraderS007(creds=_creds(token_expires_at=FAR_FUTURE),
                          on_token_refreshed=persisted.append)
        api._ensure_fresh_token()
        assert refresh_calls == [] and persisted == []
        assert api.token == "old-access"

    def test_no_refresh_token_lets_the_session_try(self, refresh_calls):
        """Not fatal: the token may still work, and the broker's own verdict
        is the useful error if it does not."""
        api = CTraderS007(creds=_creds(refresh_token=None))
        api._ensure_fresh_token()
        assert refresh_calls == []
        assert api.token == "old-access"

    def test_a_second_session_in_the_process_does_not_refresh_again(self, refresh_calls):
        api = CTraderS007(creds=_creds())
        api._ensure_fresh_token()
        api._ensure_fresh_token()
        assert len(refresh_calls) == 1

    def test_refused_refresh_surfaces_as_token_refresh_error(self, monkeypatch):
        def refuse(*_args):
            raise auth.TokenRefreshError(f"{auth.TOKEN_REQUEST_FAILED_PREFIX} (HTTP 400)")
        monkeypatch.setattr(auth, "refresh_access_token", refuse)
        api = CTraderS007(creds=_creds())
        with pytest.raises(auth.TokenRefreshError):
            api._ensure_fresh_token()


class TestEverySessionEntryPointRefreshesFirst:
    """The refresh must happen before the reactor is touched, on every path
    that opens a session."""

    class _Stop(Exception):
        pass

    def _arm(self, monkeypatch, api):
        def boom():
            raise self._Stop
        monkeypatch.setattr(api, "_ensure_fresh_token", boom)

    def test_run_refreshes_before_the_reactor(self, monkeypatch):
        api = CTraderS007(creds=_creds())
        self._arm(monkeypatch, api)
        with pytest.raises(self._Stop):
            api._run(lambda done: done(None))

    def test_persistent_session_refreshes_before_the_reactor(self, monkeypatch):
        api = CTraderS007(creds=_creds())
        self._arm(monkeypatch, api)
        with pytest.raises(self._Stop):
            api._run_persistent(on_ready=lambda: None)

    def test_s021_adapter_inherits_it(self, refresh_calls):
        persisted = []
        api = CTraderORB(creds=_creds(), on_token_refreshed=persisted.append)
        api._ensure_fresh_token()
        assert len(refresh_calls) == 1 and len(persisted) == 1


class TestSingleAccountConfigPath:
    def test_env_or_accounts_yml_creds_carry_the_refresh_material(self, monkeypatch,
                                                                  refresh_calls):
        """CTraderAdapter() with no creds dict (bot/paper.py, s007 CLI) reads
        bot.config.ctrader_credentials(), which already exposes the refresh
        fields -- the adapter must actually use them."""
        monkeypatch.setattr(legacy.C, "ctrader_credentials", lambda: _creds())
        persisted = []
        api = legacy.CTraderAdapter(on_token_refreshed=persisted.append)
        api._ensure_fresh_token()
        assert len(refresh_calls) == 1
        assert persisted[0]["access_token"] == "new-access"

    def test_s007_without_creds_uses_the_same_path(self, monkeypatch, refresh_calls):
        monkeypatch.setattr(legacy.C, "ctrader_credentials", lambda: _creds())
        api = CTraderS007()
        api._ensure_fresh_token()
        assert api.token == "new-access"


class TestAuthHelpers:
    def test_needs_human_reauth_recognises_every_credential_failure(self):
        assert auth.needs_human_reauth(
            "RuntimeError: cTrader error: account auth failed: CH_ACCESS_TOKEN_INVALID: "
            "Access token expired")
        assert auth.needs_human_reauth(
            auth.TokenRefreshError(f"{auth.TOKEN_REQUEST_FAILED_PREFIX} (HTTP 400): ACCESS_DENIED"))
        assert auth.needs_human_reauth(f"{auth.NO_REFRESH_TOKEN_PREFIX} -- re-authorise")

    def test_needs_human_reauth_ignores_ordinary_failures(self):
        assert not auth.needs_human_reauth("cTrader disconnected: connection lost")
        assert not auth.needs_human_reauth("TRADING_BAD_VOLUME: volume too small")

    def test_no_refresh_token_message_is_the_recognised_one(self):
        with pytest.raises(auth.TokenRefreshError) as caught:
            auth.refresh_access_token("cid", "sec", "")
        assert auth.needs_human_reauth(caught.value)

    def test_refresh_if_needed_honours_the_leeway(self, refresh_calls):
        in_ninety_minutes = (datetime.now(timezone.utc) + timedelta(minutes=90)).isoformat()
        creds = _creds(token_expires_at=in_ninety_minutes)
        assert auth.refresh_if_needed(creds, leeway_s=3600) is None
        assert auth.refresh_if_needed(creds, leeway_s=2 * 3600) is not None
