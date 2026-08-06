"""Montage parts must gate the peak-centered render window, not peak_start alone."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))


def test_row_window_start_prefers_clip_start() -> None:
    from shooter_vod_segment_feed import _row_window_start

    row = {
        "start": 100.0,
        "peak_start": 111.0,
        "clip": {"start": 100.0, "peak_start": 111.0, "input_duration": 22.0},
    }
    assert _row_window_start(row) == 100.0


def test_row_window_start_falls_back_without_clip() -> None:
    from shooter_vod_segment_feed import _row_window_start

    assert _row_window_start({"start": 90.0, "peak_start": 101.0}) == 90.0
    assert _row_window_start({"peak_start": 50.0}) == 50.0


def test_prepare_montage_clip_always_peak_centers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from shooter_vod_segment_feed import _prepare_montage_clip

    monkeypatch.setenv("SHOOTER_VOD_MONTAGE_PART_SEC", "14")
    monkeypatch.setenv("SHOOTER_VOD_MONTAGE_GATE_CORE_SEC", "10")
    monkeypatch.setenv("SHOOTER_VOD_MONTAGE_CORE_PAD_SEC", "2")
    vod = tmp_path / "yt_abc.mp4"
    vod.write_bytes(b"x")
    row = {
        "start": 50.0,
        "peak_start": 200.0,
        "clip": {"start": 50.0, "peak_start": 200.0, "input_duration": 18.0},
    }
    with patch("shooter_vod_segment_feed._ffprobe_duration", return_value=1000.0):
        clip = _prepare_montage_clip(row, vod, part_max=28.0)
    assert clip["peak_start"] == 200.0
    # Fight-core ship: 14s centered on peak (not 22s loot tails).
    assert clip["start"] == pytest.approx(193.0)  # 200 - 7
    assert clip["input_duration"] == pytest.approx(14.0)


def test_pick_montage_rows_returns_replacement_pool() -> None:
    from shooter_vod_segment_feed import _pick_montage_rows

    rows = [
        {"segment_id": f"x_{i}", "peak_start": float(i * 100), "score": float(10 - i)}
        for i in range(9)
    ]
    picked = _pick_montage_rows(rows, min_clips=3, max_clips=3, gap_sec=55.0)
    assert len(picked) >= 6  # max_clips * 2 at least; pool_cap = 9
    assert len(picked) == 9


def test_visual_override_includes_run_loot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from pubg_shooting_gate import pubg_passes_shooting_gate

    fake_video = tmp_path / "clip.mp4"
    fake_video.write_bytes(b"x")
    monkeypatch.setenv("PUBG_PANNS_TRUST_MIN", "0.42")
    metrics = {
        "start": 50.0,
        "duration": 22.0,
        "gunfire_density": 0.070,
        "burst_ratio": 5.5,
        "audio_rms": 0.020,
        "center_motion": 0.25,
        "center_text": 0.1,
        "crop_box": [0, 0, 100, 100],
    }
    with patch("pubg_shooting_gate.pubg_probe_segment", return_value=metrics), patch(
        "pubg_owner_calibration.pubg_passes_owner_heuristics", return_value=(True, "fight_audio")
    ), patch(
        "pubg_shooting_gate.segment_looks_like_pubg_loot_or_walk", return_value=False
    ), patch(
        "pubg_shooting_gate.segment_is_valid_for_montage",
        return_value=(False, "run_loot=motion0.25:gun0.01"),
    ):
        ok, reason, m = pubg_passes_shooting_gate(fake_video, 50.0, 22.0, panns_gun_max=0.1)
    assert ok is True
    assert "override:run_loot" in reason
    assert m.get("visual_override")
