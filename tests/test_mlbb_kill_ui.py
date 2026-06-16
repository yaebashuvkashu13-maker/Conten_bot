from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

from mlbb_kill_ui import (  # noqa: E402
    KillUiResult,
    _match_kill_keywords,
    _normalize_ocr_text,
    result_is_multikill,
)


def test_normalize_ocr_text() -> None:
    assert "double" in _normalize_ocr_text("d0uble")


def test_match_double_kill() -> None:
    count, label = _match_kill_keywords("DOUBLE KILL")
    assert count >= 1
    assert label == "double_kill"


def test_match_triple_kill_russian() -> None:
    count, label = _match_kill_keywords("Тройное убийство")
    assert count >= 1
    assert label == "triple_kill"


def test_match_savage() -> None:
    count, label = _match_kill_keywords("SAVAGE!")
    assert count >= 1
    assert label == "savage"


def test_no_match() -> None:
    count, label = _match_kill_keywords("loading screen")
    assert count == 0
    assert label == ""


def test_result_is_multikill_double() -> None:
    row = KillUiResult(0.5, True, 1, 0.1, 0.1, "DOUBLE KILL", "kill_keyword=double_kill", "double_kill")
    assert result_is_multikill(row)


def test_result_is_multikill_rejects_single() -> None:
    row = KillUiResult(0.5, True, 1, 0.1, 0.1, "slain", "kill_keyword=slain", "slain")
    assert not result_is_multikill(row)
