#!/usr/bin/env python3
"""Resolve Telegram bot credentials — prefer TG_* (prod), accept TELEGRAM_* aliases."""

from __future__ import annotations

import os
import urllib.parse
import urllib.request


def bot_token() -> str:
    return (
        os.environ.get("TG_BOT_TOKEN")
        or os.environ.get("TELEGRAM_BOT_TOKEN")
        or os.environ.get("BOT_TOKEN")
        or ""
    ).strip()


def chat_id() -> str:
    return (
        os.environ.get("TG_CHAT_ID")
        or os.environ.get("TELEGRAM_CHAT_ID")
        or os.environ.get("OWNER_CHAT_ID")
        or os.environ.get("CHAT_ID")
        or ""
    ).strip()


def credentials_ok() -> bool:
    return bool(bot_token() and chat_id())


def send_message(text: str, *, timeout: float = 20.0) -> bool:
    token = bot_token()
    chat = chat_id()
    if not token or not chat:
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    body = urllib.parse.urlencode(
        {"chat_id": chat, "text": text[:3500], "disable_web_page_preview": "1"}
    ).encode()
    try:
        urllib.request.urlopen(
            urllib.request.Request(url, data=body, method="POST"), timeout=timeout
        )
        return True
    except Exception:
        return False
