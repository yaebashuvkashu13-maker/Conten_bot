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
    clear_fast_probe_seeds,
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


def test_fast_probe_strong_single_hit_passes(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("SHOOTER_VOD_FAST_PROBE", "1")
    monkeypatch.setenv("SHOOTER_VOD_FAST_MIN_HITS", "2")
    monkeypatch.setenv("SHOOTER_VOD_FAST_STRONG_PANN", "0.40")
    monkeypatch.setenv("SHOOTER_VOD_FAST_PANN_MIN", "0.10")
    vod = tmp_path / "yt_test.mp4"
    vod.write_bytes(b"")

    def _panns(_path, _t, _w):
        return {"panns_gun_max": 0.55 if abs(_t - 120.0) < 1 else 0.02}

    sve = _mock_smart_video_editor(1500.0)
    with patch.dict(sys.modules, {"smart_video_editor": sve}):
        with patch("shooter_vod_fast_scan.score_panns_audio", side_effect=_panns):
            ok, reason, peaks = vod_fast_combat_check(vod, "pubg")
    assert ok is True
    assert "strong" in reason or peaks
    assert peaks


def test_seeds_do_not_short_circuit_stage1(monkeypatch, tmp_path: Path) -> None:
    """Regression: fast-probe seeds must merge into full stage1 (not 1-window early return)."""
    import highlight_scorer as hs

    monkeypatch.setenv("HIGHLIGHT_ALLOW_SEED_STARTS", "1")
    monkeypatch.setenv("HIGHLIGHT_SEED_STARTS", "240")
    monkeypatch.setenv("SHOOTER_VOD_SKIP_INTELLICLIP", "1")
    monkeypatch.setenv("SHOOTER_VOD_ACTION_PEAK_LIMIT", "24")
    monkeypatch.setenv("HIGHLIGHT_MAX_STAGE1", "16")
    vod = tmp_path / "yt_seed.mp4"
    vod.write_bytes(b"")

    fake_analysis = {
        "window_seconds": 2.0,
        "duration": 900.0,
        "center_motion": [0.01] * 450,
        "gunfire": [0.0] * 450,
        "audio": [0.0] * 450,
    }
    for i in (80, 120, 200, 300):
        fake_analysis["center_motion"][i] = 0.08
        fake_analysis["gunfire"][i] = 0.2

    with patch.object(hs, "_heatmap_stage0_starts", return_value=[]):
        with patch.object(hs, "owner_anchors_enabled", return_value=False):
            with patch.object(hs, "_owner_anchor_starts", return_value=[]):
                with patch.object(hs, "_owner_anchor_stage1_starts", return_value=[]):
                    with patch.object(hs, "_owner_vicinity_gun_starts", return_value=[]):
                        with patch.object(
                            hs,
                            "_action_peak_starts",
                            return_value=[160.0, 320.0, 480.0],
                        ):
                            with patch.object(
                                hs,
                                "_filter_bad_label_starts",
                                side_effect=lambda *_a, **_k: list(_a[2]),
                            ):
                                with patch(
                                    "vod_analysis_cache.analyze_video_cached",
                                    return_value=fake_analysis,
                                ):
                                    starts = hs.stage1_candidates(vod, "pubg")
    assert any(abs(s - 240.0) < 0.1 for s in starts), starts
    assert len(starts) > 1, f"expected seed merge + peaks, got {starts}"
    assert starts[0] == 240.0 or 240.0 in starts[:4]
