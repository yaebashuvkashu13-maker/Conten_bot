"""Tests for shooter VOD fast PANNs preflight."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

from shooter_vod_fast_scan import (  # noqa: E402
    apply_fast_probe_seeds,
    candidate_pool_target,
    clear_fast_probe_seeds,
    discover_montage_gun_peaks,
    vod_fast_combat_check,
)


def _mock_smart_video_editor(duration: float) -> MagicMock:
    mod = MagicMock()
    mod.ffprobe_duration = lambda _p: duration
    return mod


def test_fast_probe_skips_when_no_gun_hits(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("SHOOTER_VOD_FAST_PROBE", "1")
    vod = tmp_path / "yt_test.mp4"
    vod.write_bytes(b"")
    sve = _mock_smart_video_editor(1200.0)
    with patch.dict(sys.modules, {"smart_video_editor": sve}):
        with patch(
            "shooter_vod_fast_scan.score_panns_audio",
            return_value={"panns_gun_max": 0.05},
        ):
            ok, reason, peaks = vod_fast_combat_check(vod, "pubg")
    assert ok is False
    assert reason.startswith("fast_panns_0")
    assert peaks == []


def test_fast_probe_passes_and_seeds_env(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("SHOOTER_VOD_FAST_PROBE", "1")
    monkeypatch.setenv("SHOOTER_VOD_SEED_FROM_FAST_PROBE", "1")
    vod = tmp_path / "yt_test.mp4"
    vod.write_bytes(b"")
    def _panns(_path, _t, _w):
        return {"panns_gun_max": 0.22 if _t > 200 else 0.05}

    sve = _mock_smart_video_editor(1500.0)
    with patch.dict(sys.modules, {"smart_video_editor": sve}):
        with patch("shooter_vod_fast_scan.score_panns_audio", side_effect=_panns):
            ok, reason, peaks = vod_fast_combat_check(vod, "pubg")
    assert ok is True
    assert peaks
    assert "fast_panns_" in reason
    apply_fast_probe_seeds(peaks)
    assert os.environ.get("HIGHLIGHT_ALLOW_SEED_STARTS") == "1"
    assert os.environ.get("HIGHLIGHT_SEED_STARTS")
    clear_fast_probe_seeds()
    assert "HIGHLIGHT_SEED_STARTS" not in os.environ


def test_fast_probe_disabled(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("SHOOTER_VOD_FAST_PROBE", "0")
    vod = tmp_path / "yt_test.mp4"
    vod.write_bytes(b"")
    ok, reason, peaks = vod_fast_combat_check(vod, "pubg")
    assert ok is True
    assert reason == "fast_probe_disabled"
    assert peaks == []


def test_dense_offsets_probe_pass_shifts_grid(monkeypatch) -> None:
    from shooter_vod_fast_scan import _dense_offsets

    monkeypatch.setenv("SHOOTER_VOD_DENSE_PROBE_MAX", "8")
    monkeypatch.setenv("SHOOTER_VOD_DENSE_PROBE_STEP_SEC", "40")
    a = _dense_offsets(7200.0, skip_intro=120.0, probe_pass=0)
    b = _dense_offsets(7200.0, skip_intro=120.0, probe_pass=1)
    assert a and b
    assert a[0] != b[0] or a != b


def test_dense_offsets_have_unique_third_pass(monkeypatch) -> None:
    from shooter_vod_fast_scan import _dense_offsets

    monkeypatch.setenv("SHOOTER_VOD_DENSE_PROBE_MAX", "48")
    grids = [
        _dense_offsets(1800.0, skip_intro=60.0, probe_pass=probe_pass)
        for probe_pass in range(3)
    ]
    assert len({tuple(grid) for grid in grids}) == 3


def test_candidate_pool_can_exceed_ten_moments(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("SHOOTER_VOD_CANDIDATE_POOL_TARGET", "16")
    monkeypatch.setenv("SHOOTER_VOD_DENSE_PROBE_MAX", "48")
    vod = tmp_path / "yt_test.mp4"
    vod.write_bytes(b"")
    sve = _mock_smart_video_editor(1800.0)

    with patch.dict(sys.modules, {"smart_video_editor": sve}), patch(
        "shooter_vod_fast_scan.score_panns_audio",
        return_value={"panns_gun_max": 0.50},
    ), patch(
        "shooter_vod_fast_scan.snap_peak_to_gunfire",
        side_effect=lambda _path, center, **_kwargs: (center, 0.08, 0.50),
    ):
        peaks, reason = discover_montage_gun_peaks(
            vod,
            "pubg",
            min_clips=2,
            gap_sec=55.0,
        )

    assert candidate_pool_target(2) >= 10
    assert len(peaks) == 16
    assert "picked=16" in reason


def test_candidate_spacing_relaxes_to_fill_recall_pool(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("SHOOTER_VOD_CANDIDATE_POOL_TARGET", "16")
    monkeypatch.setenv("SHOOTER_VOD_DENSE_PROBE_MAX", "48")
    vod = tmp_path / "yt_test.mp4"
    vod.write_bytes(b"")
    sve = _mock_smart_video_editor(1800.0)
    calls = 0

    def _panns(_path, _t, _window):
        nonlocal calls
        calls += 1
        return {"panns_gun_max": 0.50 if calls <= 15 else 0.0}

    with patch.dict(sys.modules, {"smart_video_editor": sve}), patch(
        "shooter_vod_fast_scan.score_panns_audio",
        side_effect=_panns,
    ), patch(
        "shooter_vod_fast_scan.snap_peak_to_gunfire",
        side_effect=lambda _path, center, **_kwargs: (center, 0.08, 0.50),
    ):
        peaks, reason = discover_montage_gun_peaks(
            vod,
            "pubg",
            min_clips=2,
            gap_sec=55.0,
        )

    assert len(peaks) == 15
    assert "shortlist=15" in reason
