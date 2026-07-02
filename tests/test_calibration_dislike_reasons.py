"""Tests for per-game dislike reason pickers."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from calibration_dislike_reasons import (  # noqa: E402
    dislike_reason_codes,
    dislike_reason_keyboard_markup,
    normalize_game,
)


def test_pubg_has_not_metro_reason():
    codes = dislike_reason_codes("pubg")
    assert "not_metro" in codes
    assert "wrong_hero" not in codes


def test_mlbb_has_kill_reason():
    assert "no_kill" in dislike_reason_codes("mlbb")


def test_keyboard_rows():
    kb = dislike_reason_keyboard_markup("abc123", game="pubg", callback_prefix="pubg_bad")
    buttons = [b for row in kb["inline_keyboard"] for b in row]
    assert len(buttons) == 8
    assert buttons[0]["callback_data"].startswith("pubg_bad:abc123:")


def test_normalize_game():
    assert normalize_game("mobile_legends") == "mlbb"
