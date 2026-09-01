"""Variable-length shooter montage parts from gunfire sustain."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))


def _fake_analysis(duration: float = 600.0, *, peak_sec: float = 95.0, win: float = 2.0) -> dict:
    bins = int(duration / win)
    gunfire = np.zeros(bins, dtype=np.float32)
    motion = np.zeros(bins, dtype=np.float32)
    peak_idx = int(peak_sec / win)
    # Sustained fight from ~80s to ~115s (peak 95, extends right)
    for i in range(max(0, peak_idx - 8), min(bins, peak_idx + 12)):
        gunfire[i] = 0.25 + 0.1 * np.sin(i * 0.3)
        motion[i] = 0.18
    return {
        "duration": duration,
        "bins": bins,
        "window_seconds": win,
        "gunfire": gunfire,
        "center_motion": motion,
        "audio": gunfire,
    }


def test_detect_shooter_fight_bounds_extends_past_fixed_14s(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vod = tmp_path / "yt_test1234567.mp4"
    vod.write_bytes(b"fake")
    analysis = _fake_analysis(peak_sec=95.0)

    with patch("shooter_fight_segment._analysis_for", return_value=analysis):
        from shooter_fight_segment import detect_shooter_fight_bounds

        start, end, dur = detect_shooter_fight_bounds(vod, 95.0)

    assert dur > 14.0
    assert start <= 95.0 <= end
    assert end >= 105.0


def test_prepare_montage_clip_variable_length(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    vod = tmp_path / "yt_bMn6uTsDBg.mp4"
    vod.write_bytes(b"fake")
    analysis = _fake_analysis(peak_sec=95.4)
    monkeypatch.setenv("SHOOTER_VOD_VARIABLE_LENGTH", "1")

    with patch("shooter_fight_segment._analysis_for", return_value=analysis):
        with patch("shooter_vod_segment_feed._ffprobe_duration", return_value=600.0):
            from shooter_vod_segment_feed import _prepare_montage_clip

            clip = _prepare_montage_clip(
                {"peak_start": 95.4, "start": 88.4},
                vod,
                part_max=28.0,
            )

    assert float(clip["output_duration"]) > 14.0
    assert float(clip["start"]) < 95.4
    assert float(clip["fight_end"]) > 102.0


def test_montage_part_budget_three_parts_fits_55s() -> None:
    from shooter_vod_segment_feed import _montage_part_budget, _montage_limits, _montage_prefer_parts

    _min, max_clips, _gap, _part_max, final_max = _montage_limits()
    assert final_max == 55.0
    prefer = _montage_prefer_parts("pubg", max_clips, soft_min=2)
    assert prefer == 3
    budget = _montage_part_budget(prefer, final_max)
    assert 17.0 <= budget <= 20.0
    est = budget * 3 - 0.28 * 2
    assert est <= 55.5


def test_montage_part_budget_two_parts() -> None:
    from shooter_vod_segment_feed import _montage_part_budget

    budget = _montage_part_budget(2, 55.0)
    assert budget > 27.0
    assert budget * 2 - 0.28 <= 55.5


def test_fixed_length_when_disabled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    vod = tmp_path / "yt_test.mp4"
    vod.write_bytes(b"fake")
    monkeypatch.setenv("SHOOTER_VOD_VARIABLE_LENGTH", "0")

    with patch("shooter_vod_segment_feed._ffprobe_duration", return_value=600.0):
        from shooter_vod_segment_feed import _prepare_montage_clip

        clip = _prepare_montage_clip({"peak_start": 95.0}, vod, part_max=28.0)

    assert float(clip["output_duration"]) == 14.0
