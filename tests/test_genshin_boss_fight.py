"""Tests for Genshin full boss-fight window expansion."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

from genshin_boss_fight import (  # noqa: E402
    detect_boss_fight_bounds,
    expand_clip_to_full_boss_fight,
    variable_length_enabled,
)
from smart_video_editor import profile_action_clip_bounds  # noqa: E402


def test_genshin_clip_bounds_are_full_fight_scale(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SMART_GENSHIN_CLIP_MIN_SEC", raising=False)
    monkeypatch.delenv("SMART_GENSHIN_CLIP_MAX_SEC", raising=False)
    monkeypatch.delenv("GENSHIN_BOSS_FIGHT_MIN_SEC", raising=False)
    monkeypatch.delenv("GENSHIN_BOSS_FIGHT_MAX_SEC", raising=False)
    lo, hi = profile_action_clip_bounds("genshin")
    assert lo >= 25
    assert hi >= 60
    assert hi > lo


def test_variable_length_default_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GENSHIN_BOSS_FULL_FIGHT", raising=False)
    monkeypatch.delenv("SHOOTER_VOD_VARIABLE_LENGTH", raising=False)
    assert variable_length_enabled() is True
    monkeypatch.setenv("GENSHIN_BOSS_FULL_FIGHT", "0")
    assert variable_length_enabled() is False


def test_detect_boss_fight_bounds_covers_sustain(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("GENSHIN_BOSS_FIGHT_MIN_SEC", "28")
    monkeypatch.setenv("GENSHIN_BOSS_FIGHT_MAX_SEC", "90")
    monkeypatch.setenv("GENSHIN_BOSS_FIGHT_HARD_MAX_SEC", "120")
    monkeypatch.setenv("GENSHIN_BOSS_FIGHT_BAR_EXPAND", "0")
    monkeypatch.setenv("GENSHIN_VOD_LEAD_SEC", "3")

    win = 2.0
    bins = 80
    motion = np.zeros(bins, dtype=np.float32)
    audio = np.zeros(bins, dtype=np.float32)
    scene = np.zeros(bins, dtype=np.float32)
    # Active fight roughly 40s..100s (bins 20..50)
    motion[20:50] = 0.08
    audio[20:50] = 0.10
    scene[20:50] = 0.06
    analysis = {
        "window_seconds": win,
        "duration": bins * win,
        "bins": bins,
        "center_motion": motion,
        "audio": audio,
        "scene": scene,
    }
    vod = tmp_path / "yt_fake.mp4"
    vod.write_bytes(b"0")

    with patch("genshin_boss_fight._analysis_for", return_value=analysis):
        start, end, dur = detect_boss_fight_bounds(vod, peak_sec=70.0)

    assert dur >= 28
    assert start <= 70.0
    assert end >= 70.0
    # Should cover most of the sustained fight, not a 10–15s fragment.
    assert dur >= 40
    assert end - start == pytest.approx(dur, abs=0.05)


def test_expand_clip_to_full_boss_fight(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("GENSHIN_BOSS_FULL_FIGHT", "1")
    monkeypatch.setenv("GENSHIN_BOSS_FIGHT_BAR_EXPAND", "0")
    monkeypatch.setenv("GENSHIN_BOSS_FIGHT_MIN_SEC", "28")
    analysis = {
        "window_seconds": 2.0,
        "duration": 200.0,
        "bins": 100,
        "center_motion": np.full(100, 0.05, dtype=np.float32),
        "audio": np.full(100, 0.06, dtype=np.float32),
        "scene": np.full(100, 0.04, dtype=np.float32),
    }
    vod = tmp_path / "yt_fake.mp4"
    vod.write_bytes(b"0")
    with patch("genshin_boss_fight._analysis_for", return_value=analysis):
        out = expand_clip_to_full_boss_fight(
            vod,
            {"start": 50.0, "peak_start": 50.0, "input_duration": 10.0},
        )
    assert out["input_duration"] >= 28
    assert out.get("boss_fight_full") is True
    assert out["input_duration"] == out["output_duration"]
