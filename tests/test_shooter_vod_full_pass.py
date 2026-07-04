"""Tests for shooter one-pass short VOD scanning."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import numpy as np

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

from shooter_vod_full_pass import (  # noqa: E402
    _dense_gunfire_starts,
    _spread_peaks,
    shooter_skip_intro_sec,
    stage1_shooter_full_pass,
)


def test_shooter_skip_intro_short_vod() -> None:
    assert shooter_skip_intro_sec(180) == 8.0
    assert shooter_skip_intro_sec(300) == 20.0
    assert shooter_skip_intro_sec(600) == 45.0
    assert shooter_skip_intro_sec(1500) == 90.0


def test_dense_gunfire_finds_early_peak() -> None:
    win = 2.0
    bins = 120
    gun = np.zeros(bins, dtype=np.float32)
    gun[20:25] = 0.9  # ~40-50s
    analysis = {
        "window_seconds": win,
        "gunfire": gun,
        "audio": gun * 0.5,
        "center_motion": gun * 0.3,
    }
    starts = _dense_gunfire_starts(analysis, 240.0)
    assert starts
    assert min(starts) < 55.0


def test_stage1_full_pass_uses_analyze(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("SHOOTER_VOD_FULL_PASS", "1")
    vod = tmp_path / "yt_kFZA1C3Ze4s.mp4"
    vod.write_bytes(b"")
    win = 2.0
    bins = 90
    gun = np.zeros(bins, dtype=np.float32)
    gun[30:36] = 0.85
    analysis = {
        "window_seconds": win,
        "gunfire": gun,
        "audio": gun * 0.4,
        "center_motion": gun * 0.2,
        "duration": 180.0,
    }
    with patch("smart_video_editor.ffprobe_duration", return_value=180.0):
        with patch("vod_analysis_cache.analyze_video_cached", return_value=analysis):
            starts = stage1_shooter_full_pass(vod, "pubg")
    assert starts
    assert len(starts) <= 48


def test_stage1_full_pass_skips_long_vod(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("SHOOTER_VOD_FULL_PASS", "1")
    vod = tmp_path / "yt_long.mp4"
    vod.write_bytes(b"")
    with patch("smart_video_editor.ffprobe_duration", return_value=3600.0):
        assert stage1_shooter_full_pass(vod, "pubg") is None


def test_spread_peaks_respects_gap() -> None:
    win = 2.0
    bins = 100
    combined = np.zeros(bins, dtype=np.float32)
    combined[10] = 1.0
    combined[11] = 0.95
    combined[50] = 0.9
    analysis = {
        "window_seconds": win,
        "gunfire": combined,
        "audio": combined,
        "center_motion": combined,
    }
    peaks = _spread_peaks(analysis, 200.0, limit=5)
    assert len(peaks) >= 1
    if len(peaks) >= 2:
        assert abs(peaks[0] - peaks[1]) >= 22.0
