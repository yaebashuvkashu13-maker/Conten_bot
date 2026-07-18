"""Tests for vod_analysis_cache disk cache."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

from vod_analysis_cache import (  # noqa: E402
    analyze_video_cached,
    cache_key_hash,
    get_cached,
    set_cached,
)


def _fake_analysis() -> dict:
    return {
        "duration": 120.0,
        "bins": 60,
        "window_seconds": 2.0,
        "motion": np.zeros(60, dtype=np.float32),
        "center_motion": np.ones(60, dtype=np.float32) * 0.5,
        "audio": np.ones(60, dtype=np.float32) * 0.2,
        "scene": np.zeros(60, dtype=np.float32),
    }


def test_cache_hit_miss_and_mtime_invalidation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VOD_ANALYSIS_CACHE_DIR", str(tmp_path / "cache"))
    vod = tmp_path / "yt_test.mp4"
    vod.write_bytes(b"vod-bytes")

    assert get_cached(vod) is None

    analysis = _fake_analysis()
    key = set_cached(vod, analysis)
    assert key == cache_key_hash(vod)

    hit = get_cached(vod)
    assert hit is not None
    assert hit["duration"] == 120.0
    assert len(hit["center_motion"]) == 60

    time.sleep(0.02)
    vod.write_bytes(b"vod-bytes-updated")
    assert get_cached(vod) is None


def test_cache_invalidates_when_sample_fps_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("VOD_ANALYSIS_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("SMART_LONG_ANALYSIS_MAX_FPS", "0.35")
    vod = tmp_path / "yt_test.mp4"
    vod.write_bytes(b"vod-bytes")
    set_cached(vod, _fake_analysis())
    assert get_cached(vod) is not None
    monkeypatch.setenv("SMART_LONG_ANALYSIS_MAX_FPS", "1.0")
    assert get_cached(vod) is None


def test_analyze_video_cached_uses_disk(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("VOD_ANALYSIS_CACHE_DIR", str(tmp_path / "cache"))
    vod = tmp_path / "yt_test.mp4"
    vod.write_bytes(b"x")
    fake = _fake_analysis()
    calls = {"n": 0}

    def _analyze(_p: Path) -> dict:
        calls["n"] += 1
        return fake

    with patch("smart_video_editor.analyze_video", side_effect=_analyze):
        first = analyze_video_cached(vod)
        second = analyze_video_cached(vod)
    assert first["duration"] == 120.0
    assert second["duration"] == 120.0
    assert calls["n"] == 1
