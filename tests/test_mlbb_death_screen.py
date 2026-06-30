#!/usr/bin/env python3
"""Tests for MLBB death / respawn screen detection."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from mlbb_death_screen import (  # noqa: E402
    classify_death_text,
    parse_respawn_countdown,
    trim_death_tail,
)


def test_classify_death_text_ru_en() -> None:
    assert classify_death_text("You have been slain")
    assert classify_death_text("Respawn in 8 seconds")
    assert classify_death_text("Вы погибли. Ожидание возрождения")
    assert classify_death_text("Таймер возрождения 12 сек")
    assert not classify_death_text("TRIPLE KILL")
    assert not classify_death_text("")


def test_parse_respawn_countdown() -> None:
    assert parse_respawn_countdown("Respawn in 9") == 9.0
    assert parse_respawn_countdown("Ожидание возрождения 11") == 11.0
    assert parse_respawn_countdown("no timer here") is None


def test_trim_death_tail_disabled(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("MLBB_VOD_DEATH_TRIM", "0")
    vod = tmp_path / "vod.mp4"
    vod.write_bytes(b"")
    start, end, meta = trim_death_tail(vod, 10.0, 28.0, file_dur=600.0)
    assert start == 10.0
    assert end == 28.0
    assert meta == {}
