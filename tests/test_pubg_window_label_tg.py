#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import pubg_window_label_tg as win  # noqa: E402


def test_window_id_stable() -> None:
    a = win.window_id("abcdefghijk", 100.0, 115.0)
    b = win.window_id("abcdefghijk", 100.0, 115.0)
    c = win.window_id("abcdefghijk", 200.0, 215.0)
    assert a == b
    assert a != c
    assert len(a) == 12


def test_keyboards_fit_callback_limit() -> None:
    wid = "a" * 12
    yes = win.yes_no_keyboard(wid)
    assert yes["inline_keyboard"][0][0]["callback_data"].startswith("pubg_win_yes:")
    assert len(yes["inline_keyboard"][0][0]["callback_data"]) <= 64
    bad = win.bad_reason_keyboard(wid)
    for row in bad["inline_keyboard"]:
        for btn in row:
            assert len(btn["callback_data"]) <= 64
            assert btn["callback_data"].startswith("pubg_win_bad:")
