"""Tests for PUBG/Standoff multi-moment montage helpers."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from shooter_vod_montage import (  # noqa: E402
    build_montage_id,
    montage_collect_env,
    montage_enabled,
    pick_montage_rows,
    trim_idle_run_end,
    vod_richness_rank,
)


def test_montage_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SHOOTER_VOD_MONTAGE", raising=False)
    monkeypatch.delenv("PUBG_VOD_MONTAGE", raising=False)
    monkeypatch.delenv("STANDOFF_VOD_MONTAGE", raising=False)
    assert montage_enabled("pubg") is False
    # Standoff montage is on by default.
    assert montage_enabled("standoff") is True
    monkeypatch.setenv("SHOOTER_VOD_MONTAGE", "1")
    assert montage_enabled("pubg") is True
    monkeypatch.setenv("SHOOTER_VOD_MONTAGE", "0")
    monkeypatch.setenv("PUBG_VOD_MONTAGE", "1")
    assert montage_enabled("pubg") is True
    monkeypatch.setenv("STANDOFF_VOD_MONTAGE", "0")
    assert montage_enabled("standoff") is False


def test_montage_collect_env_softens_clip_score(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SHOOTER_VOD_MONTAGE", "1")
    monkeypatch.setenv("SHOOTER_VOD_MIN_CLIP_SCORE", "0.03")
    with montage_collect_env("pubg"):
        assert os.environ["SHOOTER_VOD_MIN_CLIP_SCORE"] == "0.02"
    assert os.environ["SHOOTER_VOD_MIN_CLIP_SCORE"] == "0.03"


def test_pick_montage_rows_gap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SHOOTER_VOD_MONTAGE_MIN_CLIPS", "2")
    monkeypatch.setenv("SHOOTER_VOD_MONTAGE_MAX_CLIPS", "3")
    monkeypatch.setenv("SHOOTER_VOD_MONTAGE_GAP_SEC", "50")
    rows = [
        {"start": 100, "peak_start": 110, "score": 0.4, "fight_dur": 8},
        {"start": 120, "peak_start": 130, "score": 0.5, "fight_dur": 8},
        {"start": 300, "peak_start": 310, "score": 0.35, "fight_dur": 9},
        {"start": 500, "peak_start": 510, "score": 0.3, "fight_dur": 7},
    ]
    picked = pick_montage_rows(rows)
    assert len(picked) >= 2
    peaks = [float(r["peak_start"]) for r in picked]
    for a, b in zip(peaks, peaks[1:]):
        assert b - a >= 50


def test_build_montage_id() -> None:
    assert build_montage_id("abc12345678", [{"peak_start": 88.2}, {"start": 400}]) == "abc12345678_m88_400"


def test_vod_richness_prefers_rich_pool() -> None:
    rich = {"last_pool_peaks": [100, 200, 300, 400], "zero_send_attempts": 0}
    empty = {"last_pool_peaks": [], "last_scan_at": 1.0, "zero_send_attempts": 0}
    assert vod_richness_rank(rich) < vod_richness_rank(empty)


def test_trim_idle_run_end_cuts_sprint(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SHOOTER_VOD_TRIM_RUN", "1")
    monkeypatch.setenv("SHOOTER_VOD_RUN_QUIET_BINS", "2")
    monkeypatch.setenv("SHOOTER_VOD_FIGHT_POST_SEC", "2")
    vod = tmp_path / "yt_pubg.mp4"
    vod.write_bytes(b"x")
    motion = np.array([0.2] * 8 + [0.9] * 12, dtype=np.float32)
    gunfire = np.array([0.8] * 8 + [0.05] * 12, dtype=np.float32)
    analysis = {
        "window_seconds": 1.0,
        "duration": 20.0,
        "bins": 20,
        "center_motion": motion,
        "gunfire": gunfire,
        "audio": gunfire,
        "motion": motion,
    }
    with patch("vod_analysis_cache.analyze_video_cached", return_value=analysis):
        new_end = trim_idle_run_end(vod, 0.0, 20.0, peak_sec=7.0)
    assert new_end < 20.0
    assert new_end >= 7.0 + 2.0


def test_montage_keeps_collecting_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    from highlight_scorer import _montage_keeps_collecting

    monkeypatch.setenv("SHOOTER_VOD_MONTAGE", "1")
    monkeypatch.setenv("MLBB_VOD_MONTAGE", "0")
    assert _montage_keeps_collecting("pubg") is True
    assert _montage_keeps_collecting("mobile_legends") is False
