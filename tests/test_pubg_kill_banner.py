"""Tests for PUBG kill-moment OCR discover."""

from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

from pubg_kill_banner import classify_kill_text, discover_vod_kill_moments  # noqa: E402


def test_classify_headshot_tier2() -> None:
    hit = classify_kill_text("Player1 headshot Player2")
    assert hit is not None
    assert hit.tier >= 2
    assert hit.label == "headshot"


def test_classify_eliminated_tier1() -> None:
    hit = classify_kill_text("Enemy eliminated")
    assert hit is not None
    assert hit.tier == 1
    assert hit.label == "eliminated"


def test_discover_disabled(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("PUBG_VOD_KILL_DISCOVER", "0")
    vod = tmp_path / "yt_x.mp4"
    vod.write_bytes(b"")
    assert discover_vod_kill_moments(vod, hint_peaks=[120.0]) == []


def test_classify_empty() -> None:
    assert classify_kill_text("") is None
    assert classify_kill_text("lobby waiting") is None
