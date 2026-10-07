"""utils.telegram: optional-by-design sender, HTML escaping, no token leakage."""
from __future__ import annotations

import pytest

from utils import telegram

ENV_VARS = ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")


@pytest.fixture
def unconfigured(monkeypatch):
    monkeypatch.setattr(telegram, "load_dotenv", lambda *a, **k: None)
    for name in ENV_VARS:
        monkeypatch.delenv(name, raising=False)


def test_not_configured_without_both_variables(unconfigured, monkeypatch):
    assert telegram.configured() is False
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    assert telegram.configured() is False      # chat id still missing
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    assert telegram.configured() is True


def test_send_is_a_no_op_when_unconfigured(unconfigured, monkeypatch):
    def explode(*a, **k):
        raise AssertionError("must not call the API when unconfigured")

    monkeypatch.setattr(telegram.requests, "post", explode)
    assert telegram.send("hello") is False


def test_code_escapes_html_so_a_stray_angle_bracket_cannot_break_the_markup():
    assert telegram.code("a<b&c") == "<code>a&lt;b&amp;c</code>"


def test_send_posts_html_to_the_configured_chat(unconfigured, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "secret-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "4242")
    captured = {}

    class Response:
        ok = True

    def fake_post(url, timeout, json):
        captured["url"] = url
        captured["json"] = json
        return Response()

    monkeypatch.setattr(telegram.requests, "post", fake_post)
    assert telegram.send("<b>hi</b>") is True
    assert captured["json"] == {"chat_id": "4242", "text": "<b>hi</b>",
                                "parse_mode": "HTML", "disable_web_page_preview": True}
    assert "secret-token" in captured["url"]       # token travels in the path, not the body


def test_api_errors_surface_without_echoing_the_token(unconfigured, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "secret-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "4242")

    class Response:
        ok = False
        status_code = 400
        text = '{"description":"chat not found"}'

    monkeypatch.setattr(telegram.requests, "post", lambda *a, **k: Response())
    with pytest.raises(RuntimeError) as excinfo:
        telegram.send("hi")
    assert "chat not found" in str(excinfo.value)
    assert "secret-token" not in str(excinfo.value)
