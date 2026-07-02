"""telegram_access deny-by-default."""

from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

from telegram_access import chat_is_allowed, is_owner  # noqa: E402


def test_deny_by_default() -> None:
    assert is_owner("111", "111") is True
    assert chat_is_allowed("111", owner_chat_id="111", allowed_chat_ids=set()) is True
    assert chat_is_allowed("999", owner_chat_id="111", allowed_chat_ids=set()) is False


def test_explicit_allowlist() -> None:
    allowed = {"222"}
    assert chat_is_allowed("222", owner_chat_id="111", allowed_chat_ids=allowed) is True
    assert chat_is_allowed("333", owner_chat_id="111", allowed_chat_ids=allowed) is False
