"""PUBG owner 👎 reason picker (Shorts + VOD segments)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from pubg_dislike_reasons import (  # noqa: E402
    DISLIKE_REASON_CODES,
    dislike_reason_keyboard_markup,
    dislike_reason_label,
)


def test_not_metro_first_row():
    markup = dislike_reason_keyboard_markup("abc123", callback_prefix="pubg_vseg_bad")
    rows = markup["inline_keyboard"]
    assert rows[0][0]["text"] == "🚇 Не Metro"
    assert rows[0][0]["callback_data"] == "pubg_vseg_bad:abc123:not_metro"
    assert len(rows[0]) == 1


def test_yt_prefix_stripped_in_callbacks():
    markup = dislike_reason_keyboard_markup("yt_xyz", callback_prefix="pubg_short_bad")
    flat = [btn for row in markup["inline_keyboard"] for btn in row]
    assert all(":xyz:" in btn["callback_data"] for btn in flat)


def test_all_reason_codes_present():
    markup = dislike_reason_keyboard_markup("v1", callback_prefix="pubg_short_bad")
    flat = [btn for row in markup["inline_keyboard"] for btn in row]
    codes = {btn["callback_data"].rsplit(":", 1)[-1] for btn in flat}
    assert codes == DISLIKE_REASON_CODES


def test_dislike_reason_label():
    assert dislike_reason_label("not_metro") == "🚇 Не Metro"
    assert dislike_reason_label("unknown") == "unknown"
