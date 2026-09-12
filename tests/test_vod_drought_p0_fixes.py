"""P0 drought fixes: exhaust, gun-snap, skip honor, dense deadline, gun-only timeline."""
from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import patch

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
import sys

sys.path.insert(0, str(SCRIPTS))


def test_drought_defaults_exhaust_and_gun_snap(monkeypatch: pytest.MonkeyPatch) -> None:
    from vod_force_send import apply_drought_pubg_env

    monkeypatch.delenv("VOD_FORCE_SEND_ZERO_EXHAUST", raising=False)
    monkeypatch.delenv("PUBG_OWNER_NEIGHBORHOOD_GUN_SNAP", raising=False)
    monkeypatch.delenv("SHOOTER_VOD_SKIP_DISCOVERY", raising=False)
    monkeypatch.delenv("VOD_FORCE_SKIP_DISCOVERY", raising=False)
    monkeypatch.delenv("VOD_FORCE_OPS_SKIP_DISCOVERY", raising=False)
    env = apply_drought_pubg_env({}, escalation=0)
    assert env["PUBG_SINGLES_ZERO_SEND_EXHAUST"] == "20"
    assert env["PUBG_OWNER_NEIGHBORHOOD_GUN_SNAP"] == "1"
    assert env["PUBG_DISLIKE_LOOT_FLOOR_LOCK"] == "0"
    assert env["SHOOTER_VOD_SKIP_DISCOVERY"] == "0"


def test_drought_honors_ops_skip_before_absolute_silence(monkeypatch: pytest.MonkeyPatch) -> None:
    from vod_force_send import apply_drought_pubg_env

    monkeypatch.setenv("SHOOTER_VOD_SKIP_DISCOVERY", "1")
    monkeypatch.setenv("VOD_ABSOLUTE_SILENCE_SEC", "10800")
    with patch("vod_hang_detector.last_send_age_sec", return_value=3600.0):
        env = apply_drought_pubg_env({}, escalation=2)
    assert env["SHOOTER_VOD_SKIP_DISCOVERY"] == "1"
    assert env["VOD_FORCE_SKIP_DISCOVERY"] == "1"


def test_drought_clears_ops_skip_after_absolute_silence(monkeypatch: pytest.MonkeyPatch) -> None:
    from vod_force_send import apply_drought_pubg_env

    monkeypatch.setenv("SHOOTER_VOD_SKIP_DISCOVERY", "1")
    monkeypatch.setenv("VOD_ABSOLUTE_SILENCE_SEC", "10800")
    with patch("vod_hang_detector.last_send_age_sec", return_value=12000.0):
        env = apply_drought_pubg_env({}, escalation=2)
    assert env["SHOOTER_VOD_SKIP_DISCOVERY"] == "0"
    assert env["VOD_FORCE_SKIP_DISCOVERY"] == "0"


def test_gunfire_flags_ignore_loud_score_without_gun() -> None:
    from pubg_fight_segment import _gunfire_active_flags

    timeline = [
        {"start": 0.0, "gun": 0.0, "score": 0.95},
        {"start": 2.0, "gun": 0.04, "score": 0.10},
        {"start": 4.0, "gun": 0.01, "score": 0.80},
    ]
    flags = _gunfire_active_flags(timeline, active_min=0.34)
    assert flags == [False, True, False]


def test_elasticity_does_not_harden_under_soften(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import pubg_drought_elasticity as el

    monkeypatch.setenv("PUBG_DROUGHT_ELASTICITY", "1")
    monkeypatch.setenv("PUBG_DROUGHT_ELASTICITY_PATH", str(tmp_path / "el.json"))
    monkeypatch.setenv("VOD_FORCE_SOFTEN", "1")
    monkeypatch.setenv("PUBG_QUALITY_SCORE_MIN_SINGLES", "0.20")
    # Fresh send → would be 1.10 without soften guard
    el.note_successful_send(ts=time.time())
    assert el.elasticity_scale(hours_idle=0.2) == pytest.approx(1.0)
    info = el.apply_elasticity_to_environ(hours_idle=0.2)
    assert float(info["applied"]["PUBG_QUALITY_SCORE_MIN_SINGLES"]) <= 0.20 + 1e-6


def test_dense_deadline_env_is_consulted(monkeypatch: pytest.MonkeyPatch) -> None:
    """SHOOTER_VOD_DENSE_PROBE_DEADLINE_SEC must be read by discover path."""
    src = (SCRIPTS / "shooter_vod_fast_scan.py").read_text(encoding="utf-8")
    assert "SHOOTER_VOD_DENSE_PROBE_DEADLINE_SEC" in src
    assert "_deadline_hit" in src
    batch = (SCRIPTS / "vod_audio_batch.py").read_text(encoding="utf-8")
    assert "SHOOTER_VOD_DENSE_PROBE_DEADLINE_SEC" in batch
