"""Tests for Genshin fast VOD preflight."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

from genshin_vod_fast_scan import _probe_offsets  # noqa: E402


def test_probe_offsets_accepts_short_boss_vod(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GENSHIN_BOSS_FIGHT_MIN_SEC", "28")
    # Previously: skip=120 required dur>=210 → 208s VODs falsely too_short.
    offsets = _probe_offsets(208.0, skip_intro=45.0)
    assert offsets
    assert all(t < 208.0 - 20 for t in offsets)


def test_probe_offsets_very_short_midpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GENSHIN_BOSS_FIGHT_MIN_SEC", "28")
    offsets = _probe_offsets(50.0, skip_intro=45.0)
    assert offsets  # mid-point fallback
    offsets_tiny = _probe_offsets(20.0, skip_intro=45.0)
    assert offsets_tiny == []
