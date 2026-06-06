from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

from strict_segment_gate import (  # noqa: E402
    _genshin_extra_reject,
    _standoff_extra_reject,
    format_acceptance_table,
    passes_strict_gate,
)


def test_standoff_run_segment_fails_low_motion_and_gun() -> None:
    """motion=0.073 gun=0.075 must FAIL under strict standoff floors."""
    metrics = {
        "gunfire_density": 0.075,
        "burst_ratio": 9.0,
        "center_motion": 0.073,
    }
    bad, reason = _standoff_extra_reject(metrics)
    assert bad is True
    assert "low_gunfire" in reason or "run_no_fight" in reason


def test_standoff_strict_pass() -> None:
    metrics = {
        "gunfire_density": 0.12,
        "burst_ratio": 9.0,
        "center_motion": 0.15,
    }
    bad, reason = _standoff_extra_reject(metrics)
    assert bad is False
    assert reason == ""


def test_genshin_extra_reject_weak_boss() -> None:
    metrics = {"center_motion": 0.10, "boss_score": 0.40}
    bad, reason = _genshin_extra_reject(metrics)
    assert bad is True
    assert "low_boss_motion" in reason

    metrics2 = {"center_motion": 0.20, "boss_score": 0.30}
    bad2, reason2 = _genshin_extra_reject(metrics2)
    assert bad2 is True
    assert "weak_boss_score" in reason2


def test_genshin_extra_reject_pass() -> None:
    metrics = {"center_motion": 0.20, "boss_score": 0.38}
    bad, reason = _genshin_extra_reject(metrics)
    assert bad is False


def test_acceptance_table_all_pass_flag() -> None:
    rows = [
        {"profile": "standoff", "start": 10.0, "pass": True, "gate_reason": "ok",
         "gunfire_density": 0.12, "burst_ratio": 9, "center_motion": 0.15, "audio_rms": 0.02},
        {"profile": "standoff", "start": 20.0, "pass": True, "gate_reason": "ok",
         "gunfire_density": 0.11, "burst_ratio": 8.5, "center_motion": 0.14, "audio_rms": 0.02},
    ]
    table = format_acceptance_table("Standoff", rows)
    assert "ALL_PASS=True" in table

    rows[1]["pass"] = False
    table_fail = format_acceptance_table("Standoff", rows)
    assert "ALL_PASS=False" in table_fail


@pytest.fixture()
def fake_video(tmp_path: Path) -> Path:
    path = tmp_path / "clip.mp4"
    path.write_bytes(b"\x00")
    return path


def test_passes_strict_gate_standoff_run_segment_fail(fake_video: Path) -> None:
    probe = {
        "start": 100.0,
        "duration": 9.0,
        "profile": "standoff",
        "gunfire_density": 0.075,
        "burst_ratio": 9.0,
        "center_motion": 0.073,
        "audio_rms": 0.015,
        "center_text": 0.05,
    }
    with patch("strict_segment_gate.probe_segment", return_value=probe), patch(
        "strict_segment_gate.segment_is_valid_for_montage", return_value=(True, "ok")
    ):
        ok, reason, _metrics = passes_strict_gate(fake_video, 100.0, 9.0, "standoff")
    assert ok is False
    assert "low_gunfire" in reason or "run_no_fight" in reason


def test_pubg_shooting_gate_no_shots_fail(fake_video: Path) -> None:
    from pubg_shooting_gate import pubg_passes_shooting_gate

    metrics = {
        "start": 50.0,
        "duration": 9.0,
        "gunfire_density": 0.030,
        "burst_ratio": 2.0,
        "audio_rms": 0.006,
        "center_motion": 0.04,
        "center_text": 0.1,
        "crop_box": [0, 0, 100, 100],
    }
    with patch("pubg_shooting_gate.pubg_probe_segment", return_value=metrics), patch(
        "pubg_owner_calibration.pubg_passes_owner_heuristics", return_value=(False, "no_shots")
    ), patch(
        "pubg_shooting_gate.segment_is_valid_for_montage", return_value=(True, "ok")
    ), patch(
        "pubg_shooting_gate.segment_looks_like_pubg_loot_or_walk", return_value=False
    ):
        ok, reason, _m = pubg_passes_shooting_gate(fake_video, 50.0, 9.0)
    assert ok is False
    assert "no_shots" in reason


def test_pubg_shooting_gate_strict_pass(fake_video: Path) -> None:
    from pubg_shooting_gate import pubg_passes_shooting_gate

    metrics = {
        "start": 50.0,
        "duration": 9.0,
        "gunfire_density": 0.070,
        "burst_ratio": 5.5,
        "audio_rms": 0.020,
        "center_motion": 0.05,
        "center_text": 0.1,
        "crop_box": [0, 0, 100, 100],
    }
    with patch("pubg_shooting_gate.pubg_probe_segment", return_value=metrics), patch(
        "pubg_owner_calibration.pubg_passes_owner_heuristics", return_value=(True, "fight_audio")
    ), patch(
        "pubg_shooting_gate.segment_is_valid_for_montage", return_value=(True, "ok")
    ), patch(
        "pubg_shooting_gate.segment_looks_like_pubg_loot_or_walk", return_value=False
    ):
        ok, reason, _m = pubg_passes_shooting_gate(fake_video, 50.0, 9.0)
    assert ok is True
    assert "strict_gun" in reason or "fight_audio" in reason


def test_smart_editor_blocks_send_without_strict_peak(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from smart_video_editor import send_telegram_video

    video = tmp_path / "out.mp4"
    video.write_bytes(b"x")
    monkeypatch.setenv("QUEUE_GAME_PROFILE", "pubg")
    monkeypatch.delenv("STRICT_PEAK_MONTAGE", raising=False)
    monkeypatch.delenv("ALLOW_LEGACY_MONTAGE_SEND", raising=False)
    with pytest.raises(RuntimeError, match="STRICT_PEAK_MONTAGE"):
        send_telegram_video("token", "123", video, "caption")
