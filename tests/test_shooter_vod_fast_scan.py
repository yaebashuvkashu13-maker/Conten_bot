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


def test_short_vod_gets_dense_offsets(monkeypatch) -> None:
    from shooter_vod_fast_scan import _probe_offsets

    monkeypatch.setenv("SHOOTER_VOD_FAST_PROBE_MAX", "8")
    monkeypatch.setenv("SHOOTER_VOD_FAST_PROBE_STEP_SHORT", "90")
    # ~10 min VOD must not return empty (old skip+90 gate).
    offs = _probe_offsets(600.0, skip_intro=120.0)
    assert len(offs) >= 3
    assert offs[0] <= 60.0 + 1e-6


def test_strong_single_hit_passes(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("SHOOTER_VOD_FAST_PROBE", "1")
    monkeypatch.setenv("SHOOTER_VOD_FAST_MIN_HITS", "2")
    monkeypatch.setenv("SHOOTER_VOD_FAST_STRONG_PANN", "0.40")
    monkeypatch.setenv("SHOOTER_VOD_FAST_PANN_MIN", "0.14")
    vod = tmp_path / "yt_test.mp4"
    vod.write_bytes(b"")
    seen: list[float] = []

    def _panns(_path, t, _w):
        seen.append(t)
        # Only the first probe is a strong gun hit.
        return {"panns_gun_max": 0.55 if len(seen) == 1 else 0.05}

    sve = _mock_smart_video_editor(700.0)
    with patch.dict(sys.modules, {"smart_video_editor": sve}):
        with patch("shooter_vod_fast_scan.score_panns_audio", side_effect=_panns):
            ok, reason, peaks = vod_fast_combat_check(vod, "pubg")
    assert ok is True
    assert "strong_1" in reason
    assert len(peaks) == 1
