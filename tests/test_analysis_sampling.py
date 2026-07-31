"""Tests for denser VOD analysis sampling (all games)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

from smart_video_editor import (  # noqa: E402
    analysis_detail_mode,
    analysis_effective_fps,
    analysis_sampling,
)


def test_short_vod_samples_densely(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SMART_ANALYSIS_DETAIL", "max")
    monkeypatch.setenv("SMART_SAMPLE_FPS", "4.0")
    monkeypatch.delenv("SMART_LONG_VIDEO_MIN_SEC", raising=False)
    window, fps, seek = analysis_sampling(600.0)
    assert seek is False
    assert window == 1.0
    assert fps >= 4.0


def test_long_vod_max_detail_not_throttled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SMART_ANALYSIS_DETAIL", "max")
    monkeypatch.setenv("SMART_LONG_VIDEO_MIN_SEC", "1200")
    monkeypatch.setenv("SMART_LONG_SAMPLE_FPS", "2.0")
    monkeypatch.setenv("SMART_LONG_WINDOW_SEC", "2.0")
    # Old silent throttle must not win under detail=max.
    monkeypatch.setenv("SMART_LONG_ANALYSIS_MAX_FPS", "0.35")
    window, fps, seek = analysis_sampling(1800.0)
    assert seek is True
    assert window <= 1.0
    assert fps >= 2.0
    eff = analysis_effective_fps(1800.0, fps, seek_mode=True)
    assert eff >= 2.0


def test_fast_detail_allows_lower_fps(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SMART_ANALYSIS_DETAIL", "fast")
    monkeypatch.setenv("SMART_LONG_VIDEO_MIN_SEC", "1200")
    monkeypatch.setenv("SMART_LONG_SAMPLE_FPS", "2.0")
    monkeypatch.setenv("SMART_LONG_ANALYSIS_MAX_FPS", "1.0")
    window, fps, seek = analysis_sampling(2400.0)
    assert seek is True
    assert fps <= 1.0
    assert window >= 2.0
    assert analysis_detail_mode() == "fast"


def test_cache_fingerprint_changes_with_fps(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from vod_analysis_cache import cache_key_hash

    vod = tmp_path / "x.mp4"
    vod.write_bytes(b"abc")
    monkeypatch.setenv("SMART_LONG_SAMPLE_FPS", "1.0")
    h1 = cache_key_hash(vod)
    monkeypatch.setenv("SMART_LONG_SAMPLE_FPS", "2.0")
    h2 = cache_key_hash(vod)
    assert h1 != h2
