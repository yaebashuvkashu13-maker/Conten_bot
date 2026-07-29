"""Tests for MLBB multi-moment montage helpers."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from mlbb_vod_montage import (  # noqa: E402
    build_montage_id,
    montage_collect_env,
    montage_enabled,
    pick_montage_rows,
    trim_idle_run_end,
)


def test_montage_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MLBB_SKIP_MONTAGE", raising=False)
    monkeypatch.setenv("MLBB_VOD_MONTAGE", "0")
    assert montage_enabled() is False
    monkeypatch.setenv("MLBB_VOD_MONTAGE", "1")
    assert montage_enabled() is True
    monkeypatch.setenv("MLBB_SKIP_MONTAGE", "1")
    assert montage_enabled() is False


def test_pick_montage_rows_rejects_motion_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MLBB_VOD_MONTAGE_MIN_CLIPS", "2")
    rows = [
        {"start": 100, "peak_start": 110, "kill_banner_tier": 0, "anchor": "motion", "clip_score": 0.4, "fight_dur": 12},
        {"start": 300, "peak_start": 310, "kill_banner_tier": 0, "anchor": "motion", "clip_score": 0.5, "fight_dur": 14},
        {"start": 500, "peak_start": 510, "kill_banner_tier": 0, "anchor": "motion", "clip_score": 0.3, "fight_dur": 11},
    ]
    assert pick_montage_rows(rows) == []


def test_montage_collect_env_allows_single(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MLBB_VOD_MONTAGE", "1")
    monkeypatch.setenv("MLBB_KILL_BANNER_MIN_TIER", "double")
    with montage_collect_env():
        assert os.environ["MLBB_KILL_BANNER_MIN_TIER"] == "single"
        assert os.environ["MLBB_KILL_BANNER_REQUIRED"] == "1"
        assert os.environ["MLBB_VOD_MOTION_ANCHOR_OK"] == "0"
    assert os.environ["MLBB_KILL_BANNER_MIN_TIER"] == "double"


def test_pick_montage_rows_prefers_multi_and_gap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MLBB_VOD_MONTAGE_MIN_CLIPS", "2")
    monkeypatch.setenv("MLBB_VOD_MONTAGE_MAX_CLIPS", "3")
    monkeypatch.setenv("MLBB_VOD_MONTAGE_GAP_SEC", "60")
    rows = [
        {"start": 100, "peak_start": 110, "kill_banner_tier": 1, "clip_score": 0.2, "fight_dur": 12},
        {"start": 130, "peak_start": 140, "kill_banner_tier": 1, "clip_score": 0.3, "fight_dur": 12},
        {"start": 300, "peak_start": 310, "kill_banner_tier": 2, "clip_score": 0.4, "fight_dur": 14},
        {"start": 500, "peak_start": 510, "kill_banner_tier": 1, "clip_score": 0.25, "fight_dur": 11},
    ]
    picked = pick_montage_rows(rows)
    assert len(picked) >= 2
    assert any(int(r["kill_banner_tier"]) >= 2 for r in picked)
    peaks = [float(r["peak_start"]) for r in picked]
    for a, b in zip(peaks, peaks[1:]):
        assert b - a >= 60


def test_pick_montage_rows_keeps_vod_chronology(monkeypatch: pytest.MonkeyPatch) -> None:
    """Later peaks with earlier window starts must not jump ahead in the stitch."""
    monkeypatch.setenv("MLBB_VOD_MONTAGE_MIN_CLIPS", "3")
    monkeypatch.setenv("MLBB_VOD_MONTAGE_MAX_CLIPS", "3")
    monkeypatch.setenv("MLBB_VOD_MONTAGE_GAP_SEC", "40")
    rows = [
        # peak 205 wrongly expanded to start=0 (would sort first by start)
        {"start": 0, "peak_start": 205, "kill_banner_tier": 3, "clip_score": 0.9, "fight_dur": 14},
        {"start": 90, "peak_start": 108, "kill_banner_tier": 3, "clip_score": 0.5, "fight_dur": 14},
        {"start": 1, "peak_start": 8, "kill_banner_tier": 3, "clip_score": 0.4, "fight_dur": 14},
    ]
    picked = pick_montage_rows(rows)
    assert [int(r["peak_start"]) for r in picked] == [8, 108, 205]


def test_pick_montage_rows_too_few() -> None:
    rows = [
        {"start": 100, "peak_start": 110, "kill_banner_tier": 1, "clip_score": 0.2, "fight_dur": 12},
    ]
    assert pick_montage_rows(rows) == []


def test_build_montage_id() -> None:
    rows = [{"peak_start": 120.4}, {"start": 400}]
    assert build_montage_id("abc12345678", rows) == "abc12345678_m120_400"


def test_trim_idle_run_end_cuts_sprint_tail(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MLBB_VOD_TRIM_RUN", "1")
    monkeypatch.setenv("MLBB_VOD_RUN_QUIET_BINS", "2")
    monkeypatch.setenv("MLBB_BANNER_POST_SEC", "3")
    vod = tmp_path / "yt_x.mp4"
    vod.write_bytes(b"x")
    # 20 bins × 2s: fight then sprint (low combat, high motion)
    motion = np.array([0.2] * 8 + [0.9] * 12, dtype=np.float32)
    audio = np.array([0.8] * 8 + [0.1] * 12, dtype=np.float32)
    scene = np.array([0.7] * 8 + [0.1] * 12, dtype=np.float32)
    analysis = {
        "window_seconds": 2.0,
        "duration": 40.0,
        "bins": 20,
        "center_motion": motion,
        "audio": audio,
        "scene": scene,
    }
    with patch("mlbb_fight_segment._analysis_for", return_value=analysis):
        new_end = trim_idle_run_end(vod, 0.0, 40.0, banner_sec=14.0)
    assert new_end < 40.0
    assert new_end >= 14.0 + 3.0


def test_apply_run_trim_hard_caps_after_banner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from mlbb_vod_montage import apply_run_trim_to_clip

    monkeypatch.setenv("MLBB_VOD_TRIM_RUN", "1")
    monkeypatch.setenv("MLBB_BANNER_POST_SEC", "3")
    monkeypatch.setenv("MLBB_BANNER_HARD_POST_CUT", "1")
    monkeypatch.setenv("MLBB_FIGHT_MIN_SEC", "5")
    vod = tmp_path / "yt_x.mp4"
    vod.write_bytes(b"x")
    # No combat decay signal — hard post cut must still fire.
    motion = np.ones(30, dtype=np.float32) * 0.5
    audio = np.ones(30, dtype=np.float32) * 0.5
    analysis = {
        "window_seconds": 1.0,
        "duration": 30.0,
        "bins": 30,
        "center_motion": motion,
        "audio": audio,
        "scene": audio,
    }
    clip = {
        "start": 5.0,
        "peak_start": 12.0,
        "banner_sec": 12.0,
        "input_duration": 20.0,
        "output_duration": 20.0,
        "anchor": "kill_banner",
    }
    with patch("mlbb_fight_segment._analysis_for", return_value=analysis):
        out = apply_run_trim_to_clip(clip, vod)
    assert float(out["fight_end"]) <= 15.0 + 0.05
    assert float(out["input_duration"]) <= 10.05


def test_vod_richness_prefers_rich_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("cv2")
    from mlbb_vod_segment_feed import _vod_richness_rank

    rich = {"last_pool_peaks": [100, 200, 300, 400], "zero_send_attempts": 0}
    empty = {"last_pool_peaks": [], "last_scan_at": 1.0, "zero_send_attempts": 0}
    assert _vod_richness_rank(rich) < _vod_richness_rank(empty)
