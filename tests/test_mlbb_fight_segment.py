"""Fight bounds — lead before peak and fight-until-end."""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import mlbb_fight_segment as fight  # noqa: E402


def _fake_analysis(*, duration: float = 120.0, bins: int = 60, win: float = 2.0) -> dict:
    motion = np.zeros(bins, dtype=np.float32)
    audio = np.zeros(bins, dtype=np.float32)
    scene = np.zeros(bins, dtype=np.float32)
    peak_idx = 9
    for i in range(peak_idx - 2, peak_idx + 10):
        if 0 <= i < bins:
            motion[i] = 0.9
            audio[i] = 0.7
    return {
        "window_seconds": win,
        "duration": duration,
        "bins": bins,
        "center_motion": motion,
        "audio": audio,
        "scene": scene,
    }


def test_detect_fight_bounds_lead_before_peak(monkeypatch) -> None:
    monkeypatch.setenv("MLBB_VOD_LEAD_SEC", "4")
    monkeypatch.setenv("MLBB_FIGHT_UNTIL_END", "1")
    monkeypatch.setenv("MLBB_FIGHT_MAX_SEC", "90")
    monkeypatch.setenv("MLBB_FIGHT_MIN_SEC", "10")
    vod = Path("/tmp/fake_vod.mp4")
    fake_mod = type(sys)("smart_video_editor")
    fake_mod.analyze_video = lambda _vod: _fake_analysis()
    monkeypatch.setitem(sys.modules, "smart_video_editor", fake_mod)
    start, end, dur = fight.detect_fight_bounds(vod, 18.0)
    assert start == 14.0
    assert dur >= 10.0
    assert end > start


def test_fight_until_end_disabled_caps_duration(monkeypatch) -> None:
    monkeypatch.setenv("MLBB_FIGHT_UNTIL_END", "0")
    monkeypatch.setenv("MLBB_FIGHT_MAX_SEC", "22")
    monkeypatch.setenv("MLBB_VOD_LEAD_SEC", "4")
    vod = Path("/tmp/fake_vod.mp4")
    fake_mod = type(sys)("smart_video_editor")
    fake_mod.analyze_video = lambda _vod: _fake_analysis()
    monkeypatch.setitem(sys.modules, "smart_video_editor", fake_mod)
    start, end, dur = fight.detect_fight_bounds(vod, 18.0)
    assert dur <= 22.0


def test_cached_analyze_video_reuses_mtime(monkeypatch, tmp_path: Path) -> None:
    fight.clear_analysis_cache()
    vod = tmp_path / "vod.mp4"
    vod.write_bytes(b"x")
    calls = {"n": 0}

    def _analyze(_vod):
        calls["n"] += 1
        return _fake_analysis()

    fake_mod = type(sys)("smart_video_editor")
    fake_mod.analyze_video = _analyze
    monkeypatch.setitem(sys.modules, "smart_video_editor", fake_mod)
    fight.detect_fight_bounds(vod, 18.0)
    fight.detect_fight_bounds(vod, 22.0)
    assert calls["n"] == 1
