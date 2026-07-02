#!/usr/bin/env python3
"""Tests for MLBB death / respawn screen detection."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from mlbb_death_screen import (  # noqa: E402
    classify_death_text,
    death_trim_end,
    parse_respawn_countdown,
    trim_death_tail,
)


def test_classify_death_text_ru_en() -> None:
    assert classify_death_text("You have been slain")
    assert classify_death_text("Respawn in 8 seconds")
    assert classify_death_text("Вы погибли. Ожидание возрождения")
    assert classify_death_text("Возрождение через 11 сек")
    assert classify_death_text("Вы были убиты")
    assert not classify_death_text("TRIPLE KILL")
    assert not classify_death_text("")


def test_parse_respawn_countdown_digit_hint() -> None:
    assert parse_respawn_countdown("", digit_hint=9) == 9.0
    assert parse_respawn_countdown("Возрождение через 11") == 11.0
    assert parse_respawn_countdown("8 сек") == 8.0


def test_death_trim_end_keeps_post_death_seconds() -> None:
    assert death_trim_end(10.0, 30.0, 22.0) == 26.0
    assert death_trim_end(10.0, 26.0, 22.0) is None
    assert death_trim_end(10.0, 25.0, 22.0) is None


def test_trim_death_tail_disabled(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("MLBB_VOD_DEATH_TRIM", "0")
    vod = tmp_path / "vod.mp4"
    vod.write_bytes(b"")
    start, end, meta = trim_death_tail(vod, 10.0, 28.0, file_dur=600.0)
    assert start == 10.0
    assert end == 28.0
    assert meta == {}
