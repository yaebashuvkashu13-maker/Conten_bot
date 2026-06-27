"""Tests for PUBG kill-moment OCR classification."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from pubg_kill_banner import KillMomentHit, classify_kill_text, discover_vod_kill_moments  # noqa: E402


def test_classify_eliminated_single_tier():
    hit = classify_kill_text("Player123 ELIMINATED enemy")
    assert hit is not None
    assert hit.tier == 1
    assert hit.label == "eliminated"


def test_classify_double_kill_tier2():
    hit = classify_kill_text("DOUBLE KILL")
    assert hit is not None
    assert hit.tier == 2


def test_classify_multi_feed_keywords():
    hit = classify_kill_text("kill eliminated knock headshot")
    assert hit is not None
    assert hit.tier == 2


def test_classify_russian_knock():
    hit = classify_kill_text("нокаут противника")
    assert hit is not None
    assert hit.label == "knock"


def test_discover_disabled_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("PUBG_VOD_KILL_DISCOVER", "0")
    assert discover_vod_kill_moments(tmp_path / "missing.mp4") == []
