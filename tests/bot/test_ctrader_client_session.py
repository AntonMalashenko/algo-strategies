"""Tests for bot/clients/ctrader/client.py — the single-session plumbing.

The point under test is the thing that was structurally broken before: a
Twisted reactor can only be run once per OS process, so every public call
must share ONE session instead of starting its own reactor. These tests use
a fake SDK client (no network, no broker) and assert that many sequential
calls succeed in one process, that the session is opened lazily and reused,
and that an expired token is refreshed BEFORE the session opens.
"""
import pytest

from bot.clients.ctrader import auth, client as client_module

pytest.importorskip("twisted")
pytestmark = pytest.mark.skipif(not client_module.HAVE_SDK,
                                reason="ctrader-open-api SDK not installed")

CREDS = {
    "client_id": "client-1",
    "client_secret": "secret-1",
    "access_token": "access-1",
    "account_id": 4242,
}


class FakeSdkClient:
    """Stand-in for ctrader_open_api.Client.

    Records every request it was asked to send and answers each one with a
    pre-canned response, so a test can drive the real session plumbing
    without a broker.
    """

    def __init__(self, responses=None):
        self.sent = []
        self.responses = responses or {}
        self.start_calls = 0
        self.stop_calls = 0
        self._on_connected = None
        self._on_disconnected = None

    def setConnectedCallback(self, callback):
        self._on_connected = callback

    def setDisconnectedCallback(self, callback):
        self._on_disconnected = callback

    def startService(self):
        self.start_calls += 1
        self._on_connected(self)

    def stopService(self):
        self.stop_calls += 1

    def send(self, req):
        from twisted.internet import defer

        self.sent.append(req)
        return defer.succeed(self.responses.get(type(req).__name__, object()))


@pytest.fixture
def fake_client(monkeypatch):
    """A CTraderApiClient whose transport and protobuf decoding are faked."""
    sdk = FakeSdkClient()
    monkeypatch.setattr(client_module, "Client", lambda *a, **kw: sdk)
    # Protobuf.extract() would refuse our stand-in responses; identity keeps
    # the session plumbing (the actual subject here) intact.
    monkeypatch.setattr(client_module.Protobuf, "extract", staticmethod(lambda resp: resp))
    api_client = client_module.CTraderApiClient(dict(CREDS))
    api_client.sdk = sdk
    return api_client


class TestSessionLifecycle:
    def test_open_is_lazy_and_idempotent(self, fake_client):
        assert fake_client._session_open is False
        fake_client.open()
        fake_client.open()
        assert fake_client._session_open is True
        # One connect, not one per open() call.
        assert fake_client.sdk.start_calls == 1

    def test_many_calls_share_one_session(self, fake_client):
        """The regression this whole refactor exists for: the SECOND call in a
        process used to be impossible, because each call ran its own reactor."""
        class FakeTrader:
            class trader:
                balance = 123_45

        fake_client.sdk.responses["ProtoOATraderReq"] = FakeTrader()

        balances = [fake_client.get_balance() for _ in range(5)]

        assert balances == [123.45] * 5
        assert fake_client.sdk.start_calls == 1

    def test_context_manager_opens_and_closes(self, fake_client):
        with fake_client as opened:
            assert opened._session_open is True
        assert fake_client._session_open is False
        assert fake_client.sdk.stop_calls == 1

    def test_close_without_open_is_a_no_op(self, fake_client):
        fake_client.close()
        assert fake_client.sdk.stop_calls == 0


class TestSymbolCaching:
    def test_symbol_list_is_downloaded_once_per_session(self, fake_client):
        class FakeLightSymbol:
            def __init__(self, name, symbol_id):
                self.symbolName = name
                self.symbolId = symbol_id

        class FakeSymbolsList:
            symbol = [FakeLightSymbol("GER40", 1), FakeLightSymbol("US500", 2)]

        fake_client.sdk.responses["ProtoOASymbolsListReq"] = FakeSymbolsList()

        first = fake_client.list_symbols()
        second = fake_client.list_symbols()

        assert first == second == {"GER40": 1, "US500": 2}
        symbol_requests = [req for req in fake_client.sdk.sent
                           if type(req).__name__ == "ProtoOASymbolsListReq"]
        assert len(symbol_requests) == 1

    def test_resolve_symbols_maps_many_keys_and_skips_unmatched(self, fake_client):
        class FakeLightSymbol:
            def __init__(self, name, symbol_id):
                self.symbolName = name
                self.symbolId = symbol_id

        class FakeSymbolsList:
            symbol = [FakeLightSymbol("GER40", 1)]

        fake_client.sdk.responses["ProtoOASymbolsListReq"] = FakeSymbolsList()

        resolved = fake_client.resolve_symbols({
            "DAX": ("DE40", "GER40"),        # second candidate matches
            "NASDAQ": ("USTEC", "NAS100"),   # nothing matches
        })

        # An instrument this broker does not offer is absent, not fatal --
        # one missing symbol must not fail a whole portfolio cycle.
        assert resolved == {"DAX": "GER40"}


class TestPreflightTokenRefresh:
    def test_expired_token_is_refreshed_before_connecting(self, fake_client, monkeypatch):
        fake_client.refresh_token = "refresh-1"
        fake_client.token_expires_at = None          # unknown expiry == expired
        persisted = {}
        fake_client.on_token_refreshed = persisted.update

        monkeypatch.setattr(
            auth, "refresh_access_token",
            lambda client_id, client_secret, refresh_token: auth.TokenBundle(
                access_token="access-2", refresh_token="refresh-2",
                expires_at=auth._parse_expires_at(None)))

        fake_client.open()

        assert fake_client.access_token == "access-2"
        assert persisted["access_token"] == "access-2"
        assert persisted["refresh_token"] == "refresh-2"
        # The refreshed token is what the account-auth request carried.
        account_auth = [req for req in fake_client.sdk.sent
                        if type(req).__name__ == "ProtoOAAccountAuthReq"]
        assert account_auth[0].accessToken == "access-2"

    def test_valid_token_is_not_refreshed(self, fake_client, monkeypatch):
        fake_client.refresh_token = "refresh-1"
        fake_client.token_expires_at = (
            auth.TokenBundle("t", "r", auth._parse_expires_at(None)).expires_at
            .replace(year=2999).isoformat())

        def explode(*_args, **_kwargs):
            raise AssertionError("must not refresh a still-valid token")

        monkeypatch.setattr(auth, "refresh_access_token", explode)
        fake_client.open()
        assert fake_client.access_token == "access-1"

    def test_missing_refresh_token_does_not_block_the_session(self, fake_client, monkeypatch):
        """Accounts authorised before refresh tokens were stored must keep
        working: let the broker itself rule on the current access token."""
        fake_client.refresh_token = None
        fake_client.token_expires_at = None

        def explode(*_args, **_kwargs):
            raise AssertionError("nothing to refresh with")

        monkeypatch.setattr(auth, "refresh_access_token", explode)
        fake_client.open()
        assert fake_client._session_open is True


class TestErrorSurfacing:
    def test_account_auth_rejection_is_raised_not_swallowed(self, fake_client):
        class ProtoOAErrorRes:
            errorCode = auth.EXPIRED_TOKEN_ERROR_CODE
            description = "Access token expired"

        fake_client.sdk.responses["ProtoOAAccountAuthReq"] = ProtoOAErrorRes()

        with pytest.raises(RuntimeError) as excinfo:
            fake_client.open()

        assert auth.is_expired_token_error(excinfo.value)
        assert fake_client._session_open is False
