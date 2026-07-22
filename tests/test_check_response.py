"""Fix 2 regression test: CTraderS007._check_response must not treat an
undecoded broker rejection as success.

Confirmed live 2026-07-20: a TRADING_BAD_VOLUME rejection came back as a raw,
undecoded envelope (payloadType + payload bytes) instead of a resolved
ProtoOAOrderErrorEvent, and the old _check_response silently returned it as
"ok". This test installs a minimal fake `ctrader_open_api` package into
sys.modules so bot.ctrader_s007 imports for real (no network, no actual SDK
needed) and exercises the SHIPPED _check_response method directly against
each response shape it must handle.
"""
from __future__ import annotations

import sys
import types

import pytest


class ProtoOAOrderErrorEvent:
    """Stands in for the real message of the same name -- same two fields
    _check_response reads (errorCode, description), a toy wire format for
    ParseFromString (b"ORDER_ERR:<code>:<description>"). Named exactly like
    the real class (not aliased) so type(msg).__name__ matching in
    _check_response works without relying on reassigning __name__, which
    doesn't affect type(instance).__name__ in Python."""

    def __init__(self, errorCode="", description=""):
        self.errorCode = errorCode
        self.description = description

    def ParseFromString(self, data: bytes) -> None:
        if not isinstance(data, (bytes, bytearray)) or not data.startswith(b"ORDER_ERR:"):
            raise ValueError("not an order-error-shaped payload")
        _, code, desc = data.split(b":", 2)
        self.errorCode, self.description = code.decode(), desc.decode()


class ProtoOAErrorRes:
    def __init__(self, errorCode="", description=""):
        self.errorCode = errorCode
        self.description = description

    def ParseFromString(self, data: bytes) -> None:
        if not isinstance(data, (bytes, bytearray)) or not data.startswith(b"ERROR_RES:"):
            raise ValueError("not an error-res-shaped payload")
        _, code, desc = data.split(b":", 2)
        self.errorCode, self.description = code.decode(), desc.decode()


class _RawEnvelope:
    """The undecoded shape Protobuf.extract() falls back to when it can't
    resolve a payloadType to a registered message class -- has payloadType
    and payload, but (crucially) no errorCode."""
    def __init__(self, payloadType: int, payload: bytes):
        self.payloadType = payloadType
        self.payload = payload


class _ExecutionEvent:
    """A normal decoded success message (order accepted) -- no payloadType/
    payload/errorCode fields at all, must pass through untouched."""
    def __init__(self, executionType="ORDER_ACCEPTED"):
        self.executionType = executionType


def _install_fake_sdk(monkeypatch):
    """Install a minimal fake ctrader_open_api so bot.ctrader / bot.ctrader_s007
    import for real against it (no network, no pip package needed)."""
    fake_pkg = types.ModuleType("ctrader_open_api")
    fake_pkg.Client = object
    fake_pkg.Protobuf = types.SimpleNamespace(extract=lambda resp: resp)
    fake_pkg.TcpProtocol = object
    fake_pkg.EndPoints = types.SimpleNamespace(
        PROTOBUF_DEMO_HOST="demo", PROTOBUF_PORT=5035)

    fake_messages_pkg = types.ModuleType("ctrader_open_api.messages")

    fake_open_api_messages = types.ModuleType("ctrader_open_api.messages.OpenApiMessages_pb2")
    for name in ("ProtoOAApplicationAuthReq", "ProtoOAAccountAuthReq",
                 "ProtoOAGetTrendbarsReq", "ProtoOASymbolsListReq",
                 "ProtoOANewOrderReq", "ProtoOACancelOrderReq", "ProtoOAReconcileReq",
                 "ProtoOATraderReq", "ProtoOAGetAccountListByAccessTokenReq",
                 "ProtoOAClosePositionReq", "ProtoOASymbolByIdReq"):
        setattr(fake_open_api_messages, name, type(name, (), {}))
    fake_open_api_messages.ProtoOAOrderErrorEvent = ProtoOAOrderErrorEvent
    fake_open_api_messages.ProtoOAErrorRes = ProtoOAErrorRes

    fake_model_messages = types.ModuleType("ctrader_open_api.messages.OpenApiModelMessages_pb2")
    for name in ("ProtoOAOrderType", "ProtoOATradeSide", "ProtoOATrendbarPeriod"):
        setattr(fake_model_messages, name, types.SimpleNamespace(BUY=1, SELL=2, M1=1, M15=15))

    modules = {
        "ctrader_open_api": fake_pkg,
        "ctrader_open_api.messages": fake_messages_pkg,
        "ctrader_open_api.messages.OpenApiMessages_pb2": fake_open_api_messages,
        "ctrader_open_api.messages.OpenApiModelMessages_pb2": fake_model_messages,
        "twisted": types.ModuleType("twisted"),
        "twisted.internet": types.SimpleNamespace(reactor=None, defer=types.SimpleNamespace(
            inlineCallbacks=lambda f: f)),
    }
    for name, mod in modules.items():
        monkeypatch.setitem(sys.modules, name, mod)
    # bot.ctrader / bot.ctrader_s007 may already be cached from an earlier
    # (real-SDK-less) import elsewhere in the test session -- drop them so
    # they re-import against our fake SDK.
    monkeypatch.delitem(sys.modules, "bot.ctrader", raising=False)
    monkeypatch.delitem(sys.modules, "bot.ctrader_s007", raising=False)


@pytest.fixture
def check_response(monkeypatch):
    _install_fake_sdk(monkeypatch)
    from bot.ctrader_s007 import CTraderS007
    yield CTraderS007._check_response


def test_decoded_order_error_event_raises(check_response):
    msg = ProtoOAOrderErrorEvent(errorCode="TRADING_BAD_STOPS", description="bad TP")
    with pytest.raises(RuntimeError, match="TRADING_BAD_STOPS"):
        check_response(msg)


def test_decoded_error_res_raises(check_response):
    msg = ProtoOAErrorRes(errorCode="ACCOUNT_DISABLED", description="disabled")
    with pytest.raises(RuntimeError, match="ACCOUNT_DISABLED"):
        check_response(msg)


def test_normal_success_message_passes_through(check_response):
    msg = _ExecutionEvent()
    assert check_response(msg) is msg


def test_undecoded_envelope_with_recoverable_order_error_raises(check_response):
    # The confirmed 2026-07-20 shape: Protobuf.extract() couldn't resolve
    # payloadType 2132 to ProtoOAOrderErrorEvent and handed back the raw
    # envelope instead. The old _check_response returned this as "success".
    msg = _RawEnvelope(payloadType=2132, payload=b"ORDER_ERR:TRADING_BAD_VOLUME:volume too big")
    with pytest.raises(RuntimeError, match="TRADING_BAD_VOLUME"):
        check_response(msg)


def test_undecoded_envelope_with_recoverable_error_res_raises(check_response):
    msg = _RawEnvelope(payloadType=2142, payload=b"ERROR_RES:MARKET_CLOSED:market is closed")
    with pytest.raises(RuntimeError, match="MARKET_CLOSED"):
        check_response(msg)


def test_undecoded_envelope_with_unrecognized_payload_raises_loud_not_silent(check_response):
    # Neither known error shape parses -- must NOT fall through as success.
    msg = _RawEnvelope(payloadType=9999, payload=b"garbage-nobody-recognizes")
    with pytest.raises(RuntimeError, match="unrecognized broker response"):
        check_response(msg)
