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


def test_fast_probe_requires_min_hits(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("SHOOTER_VOD_FAST_PROBE", "1")
    monkeypatch.setenv("SHOOTER_VOD_FAST_MIN_HITS", "2")
    vod = tmp_path / "yt_test.mp4"
    vod.write_bytes(b"")
    sve = _mock_smart_video_editor(1500.0)
    n = {"i": 0}

    def _panns(_path, _t, _w):
        n["i"] += 1
        return {"panns_gun_max": 0.20 if n["i"] == 1 else 0.05}

    with patch.dict(sys.modules, {"smart_video_editor": sve}):
        with patch("shooter_vod_fast_scan.score_panns_audio", side_effect=_panns):
            ok, reason, peaks = vod_fast_combat_check(vod, "pubg")
    assert ok is False
    assert "min_hits=2" in reason
    monkeypatch.setenv("SHOOTER_VOD_FAST_PROBE", "0")
    vod = tmp_path / "yt_test.mp4"
    vod.write_bytes(b"")
    ok, reason, peaks = vod_fast_combat_check(vod, "pubg")
    assert ok is True
    assert reason == "fast_probe_disabled"
    assert peaks == []
