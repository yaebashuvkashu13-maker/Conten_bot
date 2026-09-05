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


def test_candidate_pool_scales_beyond_fixed_top_n(monkeypatch, tmp_path: Path) -> None:
    """Long VODs must keep more than a fixed top-16 so tail fights survive."""
    monkeypatch.setenv("SHOOTER_VOD_AUDIO_GENERATOR", "0")
    monkeypatch.setenv("SHOOTER_VOD_CANDIDATE_POOL_TARGET", "16")
    monkeypatch.setenv("SHOOTER_VOD_DENSE_PROBE_MAX", "48")
    monkeypatch.setenv("PUBG_COMBAT_TIMELINE", "0")  # isolate pool sizing from merge
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

    pool = candidate_pool_target(2, duration=1800.0)
    assert pool >= 16
    assert len(peaks) >= 16
    assert len(peaks) == pool
    assert peaks[-1] > 900  # recall reaches the back half of a 30min VOD


def test_candidate_spacing_relaxes_to_fill_recall_pool(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("SHOOTER_VOD_AUDIO_GENERATOR", "0")
    monkeypatch.setenv("SHOOTER_VOD_CANDIDATE_POOL_TARGET", "16")
    monkeypatch.setenv("SHOOTER_VOD_DENSE_PROBE_MAX", "48")
    monkeypatch.setenv("PUBG_COMBAT_TIMELINE", "0")
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
    assert "shortlist=15" in reason or "picked=15" in reason or "hits=15" in reason


def test_snap_peak_runs_panns_once(monkeypatch, tmp_path: Path) -> None:
    from shooter_vod_fast_scan import snap_peak_to_gunfire

    vod = tmp_path / "yt_test.mp4"
    vod.write_bytes(b"vod")
    with patch(
        "gameplay_gate.score_pubg_gunfire_audio",
        side_effect=lambda _path, start, _dur: (0.10 if start >= 100 else 0.02, 5.0, 0.03),
    ) as gun, patch(
        "shooter_vod_fast_scan.score_panns_audio",
        return_value={"panns_gun_max": 0.4},
    ) as panns:
        center, density, pmax = snap_peak_to_gunfire(
            vod,
            100.0,
            duration=1000.0,
        )
    assert gun.call_count > 1
    assert panns.call_count == 1
    assert density == 0.10
    assert pmax == 0.4
    assert center >= 100


def test_audio_snap_skips_redundant_panns(monkeypatch, tmp_path: Path) -> None:
    from shooter_vod_fast_scan import snap_peak_to_gunfire

    vod = tmp_path / "yt_test.mp4"
    vod.write_bytes(b"vod")
    with patch(
        "gameplay_gate.score_pubg_gunfire_audio",
        return_value=(0.08, 5.0, 0.03),
    ), patch("shooter_vod_fast_scan.score_panns_audio") as panns:
        _center, density, pmax = snap_peak_to_gunfire(
            vod,
            100.0,
            duration=1000.0,
            confirm_panns=False,
        )
    panns.assert_not_called()
    assert density == 0.08
    assert pmax == 0.0


def test_audio_generator_finds_transients_across_timeline() -> None:
    from shooter_vod_fast_scan import _rank_audio_windows
    import numpy as np

    sample_rate = 1000
    pcm = np.zeros(sample_rate * 60, dtype=np.int16)
    for second in (10, 30, 50):
        start = second * sample_rate
        pcm[start : start + 80] = 30000
        pcm[start + 300 : start + 360] = -28000
    peaks = _rank_audio_windows(
        pcm,
        sample_rate=sample_rate,
        base_sec=0,
        max_candidates=10,
        gap_sec=8,
    )
    assert any(abs(peak - 10) <= 4 for peak in peaks)
    assert any(abs(peak - 30) <= 4 for peak in peaks)
    assert any(abs(peak - 50) <= 4 for peak in peaks)


def test_audio_generator_preserves_quiet_timeline_chunks() -> None:
    from shooter_vod_fast_scan import _rank_audio_windows
    import numpy as np

    sample_rate = 1000
    pcm = np.zeros(sample_rate * 900, dtype=np.int16)
    # Many loud events in chunk 0 must not evict the quieter event in chunk 2.
    for second in range(20, 280, 20):
        pcm[second * sample_rate : second * sample_rate + 100] = 30000
    pcm[700 * sample_rate : 700 * sample_rate + 70] = 18000
    peaks = _rank_audio_windows(
        pcm,
        sample_rate=sample_rate,
        base_sec=0,
        max_candidates=9,
        gap_sec=8,
        chunk_sec=300,
    )
    assert any(abs(peak - 700) <= 4 for peak in peaks)


def test_audio_generator_keeps_low_panns_candidates(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("SHOOTER_VOD_AUDIO_GENERATOR", "1")
    monkeypatch.setenv("SHOOTER_VOD_CANDIDATE_POOL_TARGET", "10")
    monkeypatch.setenv("PUBG_COMBAT_TIMELINE", "0")
    vod = tmp_path / "yt_test.mp4"
    vod.write_bytes(b"vod")
    sve = _mock_smart_video_editor(1200.0)
    centers = [100.0 + index * 40 for index in range(12)]
    with patch.dict(sys.modules, {"smart_video_editor": sve}), patch(
        "shooter_vod_fast_scan.discover_audio_candidate_offsets",
        return_value=centers,
    ), patch(
        "panns_audio_cache.prewarm_grid",
        return_value=len(centers),
    ), patch(
        "shooter_vod_fast_scan.score_panns_audio",
        return_value={"panns_gun_max": 0.01},
    ), patch(
        "shooter_vod_fast_scan.snap_peak_to_gunfire",
        side_effect=lambda _path, center, **_kwargs: (center, 0.08, 0.05),
    ):
        peaks, reason = discover_montage_gun_peaks(
            vod,
            "pubg",
            min_clips=2,
            gap_sec=55.0,
        )
    # Duration-scaled pool may keep all audio seeds; never drop below the old floor.
    assert len(peaks) >= 10
    assert len(peaks) <= len(centers)
    assert "audio_generator" in reason


def test_dense_scan_span_caps_long_vod(monkeypatch):
    from shooter_vod_fast_scan import dense_scan_span

    monkeypatch.setenv("SHOOTER_VOD_DENSE_PCM_MAX_SEC", "4200")
    # 2.7h VOD would previously extract ~9974s of PCM and freeze the feed.
    assert dense_scan_span(10034.0, 60.0) == 4200.0
    assert dense_scan_span(2400.0, 60.0) < 2400.0
    assert dense_scan_span(2400.0, 60.0) == 2400.0 - 60.0 - 12.0
