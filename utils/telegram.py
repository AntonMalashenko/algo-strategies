"""Minimal Telegram sender for operational messages (shared by any strategy tool).

Configuration lives in the environment and never on the command line:

    TELEGRAM_BOT_TOKEN    from @BotFather
    TELEGRAM_CHAT_ID      your own chat id -- message the bot once, then read it from
                          https://api.telegram.org/bot<TOKEN>/getUpdates

`send()` returns False when the bot is not configured, so a caller can treat Telegram
as an optional extra rather than a hard dependency.

HTML is the parse mode of choice here: numbers contain dots, which MarkdownV2 would
require escaping everywhere outside code spans. Text inside <code> renders monospace
and is tap-to-copy in the mobile clients.
"""
from __future__ import annotations

import html
import os
import sys
from pathlib import Path

import requests
from dotenv import load_dotenv

API_URL = "https://api.telegram.org/bot{token}/sendMessage"
UPDATES_URL = "https://api.telegram.org/bot{token}/getUpdates"
HTTP_TIMEOUT_S = 15
# Pinned to the repo root rather than discovered: dotenv's own search walks the caller's
# stack and fails outright under `python -c` / stdin, where there is no caller frame.
ENV_PATH = Path(__file__).resolve().parent.parent / ".env"


def configured() -> bool:
    load_dotenv(ENV_PATH)
    return bool(os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID"))


def code(value: str | float) -> str:
    """A tap-to-copy monospace span."""
    return f"<code>{html.escape(str(value))}</code>"


def send(text: str) -> bool:
    """Send `text` as HTML. False if the bot is not configured; raises on API errors."""
    if not configured():
        return False
    response = requests.post(
        API_URL.format(token=os.environ["TELEGRAM_BOT_TOKEN"]), timeout=HTTP_TIMEOUT_S,
        json={"chat_id": os.environ["TELEGRAM_CHAT_ID"], "text": text,
              "parse_mode": "HTML", "disable_web_page_preview": True})
    if not response.ok:
        # The token is in the URL, never in the body -- safe to surface the response text.
        raise RuntimeError(f"telegram sendMessage failed: HTTP {response.status_code} "
                           f"{response.text[:200]}")
    return True


def discover_chat_ids() -> list[tuple[str, str]]:
    """(chat_id, who) for every chat that has messaged the bot recently.

    Telegram only keeps undelivered updates for 24h, so say Start to the bot first.
    """
    load_dotenv(ENV_PATH)
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set (see .env.example)")
    response = requests.get(UPDATES_URL.format(token=token), timeout=HTTP_TIMEOUT_S)
    if not response.ok:
        raise RuntimeError(f"telegram getUpdates failed: HTTP {response.status_code} "
                           f"{response.text[:200]}")
    found: dict[str, str] = {}
    for update in response.json().get("result", []):
        chat = (update.get("message") or update.get("channel_post") or {}).get("chat")
        if chat:
            name = chat.get("username") or chat.get("title") or chat.get("first_name") or "?"
            found[str(chat["id"])] = f"{name} ({chat.get('type', '?')})"
    return sorted(found.items())


def _main() -> int:
    """Setup helper: list the chat ids, then send a test message if one is configured."""
    try:
        chats = discover_chat_ids()
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if not chats:
        print("no chats yet -- open the bot in Telegram, press Start, send it any message")
        return 1
    for chat_id, who in chats:
        print(f"TELEGRAM_CHAT_ID={chat_id}   {who}")
    if send("<b>Algo Desk</b> is wired up."):
        print("\ntest message sent")
    else:
        print("\nTELEGRAM_CHAT_ID is not set yet -- copy the line above into .env")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
