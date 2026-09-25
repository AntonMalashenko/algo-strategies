"""Tests for bot/clients/ctrader/auth.py — token expiry bookkeeping and refresh.

No network: `_request_token` is the only place that talks HTTP and it is
stubbed, so these tests cover the logic that decides WHEN to refresh and what
to persist afterwards — which is where an outage would come from.
"""
from datetime import datetime, timedelta, timezone

import pytest

from bot.clients.ctrader import auth


def _bundle(expires_in_s: int, refresh_token: str | None = "refresh-1") -> auth.TokenBundle:
    return auth.TokenBundle(
        access_token="access-1",
        refresh_token=refresh_token,
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=expires_in_s),
    )


class TestNeedsRefresh:
    def test_fresh_token_is_left_alone(self):
        assert _bundle(expires_in_s=auth.REFRESH_LEEWAY_S * 10).needs_refresh() is False

    def test_expired_token_needs_refresh(self):
        assert _bundle(expires_in_s=-1).needs_refresh() is True

    def test_token_inside_the_leeway_needs_refresh(self):
        """The whole point of the leeway: a token that is still technically
        valid but would die mid-session is renewed BEFORE the session opens."""
        assert _bundle(expires_in_s=auth.REFRESH_LEEWAY_S // 2).needs_refresh() is True


class TestCredentialsRoundTrip:
    def test_round_trip_preserves_expiry(self):
        original = _bundle(expires_in_s=auth.REFRESH_LEEWAY_S * 10)
        restored = auth.TokenBundle.from_credentials(original.as_credentials())
        assert restored.access_token == original.access_token
        assert restored.refresh_token == original.refresh_token
        assert restored.expires_at == original.expires_at
        assert restored.needs_refresh() is False

    def test_missing_access_token_yields_no_bundle(self):
        assert auth.TokenBundle.from_credentials({}) is None

    def test_unknown_expiry_is_treated_as_expired(self):
        """An account authorised before expiry was recorded must refresh on
        its next open rather than be trusted indefinitely."""
        restored = auth.TokenBundle.from_credentials({"access_token": "access-1"})
        assert restored.needs_refresh() is True

    def test_unparseable_expiry_is_treated_as_expired(self):
        restored = auth.TokenBundle.from_credentials(
            {"access_token": "access-1", "token_expires_at": "not-a-date"})
        assert restored.needs_refresh() is True

    def test_naive_stored_expiry_is_read_as_utc(self):
        future_naive = (datetime.now(timezone.utc)
                        + timedelta(seconds=auth.REFRESH_LEEWAY_S * 10)).replace(tzinfo=None)
        restored = auth.TokenBundle.from_credentials(
            {"access_token": "access-1", "token_expires_at": future_naive.isoformat()})
        assert restored.needs_refresh() is False


class TestRefreshAccessToken:
    def test_refresh_sends_the_refresh_grant(self, monkeypatch):
        captured = {}

        def fake_request_token(params):
            captured.update(params)
            return _bundle(expires_in_s=auth.REFRESH_LEEWAY_S * 10, refresh_token="refresh-2")

        monkeypatch.setattr(auth, "_request_token", fake_request_token)
        bundle = auth.refresh_access_token("client-1", "secret-1", "refresh-1")

        assert captured["grant_type"] == auth.GRANT_REFRESH_TOKEN
        assert captured["refresh_token"] == "refresh-1"
        assert bundle.refresh_token == "refresh-2"

    def test_rotated_refresh_token_is_kept(self, monkeypatch):
        """The broker may hand back a NEW refresh token; reusing the old one
        after a rotation fails and re-creates the outage."""
        monkeypatch.setattr(auth, "_request_token",
                            lambda params: _bundle(expires_in_s=60, refresh_token="rotated"))
        assert auth.refresh_access_token("c", "s", "old").refresh_token == "rotated"

    def test_absent_refresh_token_in_answer_keeps_the_old_one(self, monkeypatch):
        monkeypatch.setattr(auth, "_request_token",
                            lambda params: _bundle(expires_in_s=60, refresh_token=None))
        assert auth.refresh_access_token("c", "s", "old").refresh_token == "old"

    def test_missing_refresh_token_is_an_actionable_error(self):
        with pytest.raises(auth.TokenRefreshError, match="re-authorise"):
            auth.refresh_access_token("c", "s", "")


class TestExpiredTokenDetection:
    def test_detects_the_brokers_own_error_code(self):
        error = RuntimeError("account auth failed: CH_ACCESS_TOKEN_INVALID: Access token expired")
        assert auth.is_expired_token_error(error) is True

    def test_detects_it_in_a_plain_string(self):
        assert auth.is_expired_token_error("CH_ACCESS_TOKEN_INVALID") is True

    def test_other_broker_errors_are_not_token_errors(self):
        assert auth.is_expired_token_error(RuntimeError("MARKET_CLOSED")) is False
