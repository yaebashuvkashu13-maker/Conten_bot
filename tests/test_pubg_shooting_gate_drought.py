
import os
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))


def _clear_drought_elasticity(monkeypatch):
    for key in (
        "VOD_FORCE_SOFTEN",
        "VOD_FORCE_ESCALATION",
        "SHOOTER_VOD_SOFTEN_LEVEL",
        "PUBG_ADAPTIVE_DROUGHT_RESCUE",
        "PUBG_DROUGHT_ELASTICITY_ACTIVE",
        "PUBG_DROUGHT_ELASTICITY",
        "PUBG_DROUGHT_PANNS_OVERRIDE",
        "PUBG_DROUGHT_GUN_FACTOR",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("PUBG_DROUGHT_ELASTICITY", "0")
    monkeypatch.setenv("PUBG_DROUGHT_PANNS_OVERRIDE", "0.45")
    monkeypatch.setenv("PUBG_DROUGHT_GUN_FACTOR", "0.70")

def test_min_gunfire_respects_drought_soften(monkeypatch):
    monkeypatch.setenv("VOD_FORCE_SOFTEN", "1")
    monkeypatch.setenv("SMART_PUBG_MIN_GUNFIRE_DENSITY", "0.010")
    from pubg_shooting_gate import _min_gunfire

    assert _min_gunfire() == 0.010


def test_min_gunfire_keeps_quality_floor_steady(monkeypatch):
    _clear_drought_elasticity(monkeypatch)
    monkeypatch.delenv("VOD_FORCE_SOFTEN", raising=False)
    monkeypatch.delenv("VOD_FORCE_ESCALATION", raising=False)
    monkeypatch.setenv("SMART_PUBG_MIN_GUNFIRE_DENSITY", "0.010")
    from pubg_shooting_gate import QUALITY_FLOOR_GUNFIRE, _min_gunfire

    assert _min_gunfire() == QUALITY_FLOOR_GUNFIRE


def test_run_fake_gun_not_overridden_by_softened_strict_audio(monkeypatch):
    _clear_drought_elasticity(monkeypatch)
    """_-HbZ0zNDOs_2538: soften made strict_audio true at gun~0.05 and overrode loot run."""
    monkeypatch.setenv("VOD_FORCE_SOFTEN", "1")
    monkeypatch.setenv("SMART_PUBG_MIN_GUNFIRE_DENSITY", "0.010")
    monkeypatch.setenv("SMART_PUBG_MIN_BURST_RATIO", "3.0")
    from pubg_shooting_gate import pubg_passes_shooting_gate

    probe = {
        "start": 2538.0,
        "duration": 24.5,
        "gunfire_density": 0.056,
        "burst_ratio": 5.227,
        "audio_rms": 0.031,
        "center_motion": 0.201,
        "center_text": 0.204,
        "crop_box": None,
    }
    with patch("pubg_shooting_gate.pubg_probe_segment", return_value=probe), patch(
        "pubg_owner_calibration.pubg_passes_owner_heuristics",
        return_value=(True, "panns_trust=0.740"),
    ), patch(
        "pubg_shooting_gate.segment_looks_like_pubg_loot_or_walk", return_value=False
    ), patch(
        "pubg_shooting_gate.segment_is_valid_for_montage",
        return_value=(False, "run_fake_gun=motion0.201:gun0.056"),
    ), patch(
        "pubg_owner_calibration.segment_overlaps_owner_label", return_value=False
    ):
        ok, reason, metrics = pubg_passes_shooting_gate(
            Path("vod.mp4"), 2538.0, 24.5, panns_gun_max=0.40
        )
    assert ok is False
    assert "run_fake_gun" in reason
    assert not metrics.get("visual_override")
    assert not metrics.get("panns_visual_override")


def test_run_fake_gun_overridden_when_panns_trust_and_gun_above_fake_ceil(monkeypatch):
    """ADS fight: montage gate says run_fake_gun, but panns_trust + gun>=0.060 may pass."""
    from pubg_shooting_gate import pubg_passes_shooting_gate

    probe = {
        "start": 464.78,
        "duration": 23.22,
        "gunfire_density": 0.068,
        "burst_ratio": 4.36,
        "audio_rms": 0.05,
        "center_motion": 0.194,
        "center_text": 0.2,
        "crop_box": None,
    }
    with patch("pubg_shooting_gate.pubg_probe_segment", return_value=probe), patch(
        "pubg_owner_calibration.pubg_passes_owner_heuristics",
        return_value=(True, "panns_trust=0.685"),
    ), patch(
        "pubg_shooting_gate.segment_looks_like_pubg_loot_or_walk", return_value=False
    ), patch(
        "pubg_shooting_gate.segment_is_valid_for_montage",
        return_value=(False, "run_fake_gun=motion0.194:gun0.068"),
    ), patch(
        "pubg_owner_calibration.segment_overlaps_owner_label", return_value=False
    ):
        ok, reason, metrics = pubg_passes_shooting_gate(
            Path("vod.mp4"), 464.78, 23.22, panns_gun_max=0.685
        )
    assert ok is True
    assert metrics.get("panns_visual_override")
    assert "panns_override" in reason


def test_owner_style_overrides_diluted_run_fake_gun(monkeypatch):
    """FxTv@30 long window: owner good + panns~0.42 + diluted gun~0.035 may pass."""
    from pubg_shooting_gate import pubg_passes_shooting_gate

    probe = {
        "start": 30.8,
        "duration": 51.7,
        "gunfire_density": 0.035,
        "burst_ratio": 6.1,
        "audio_rms": 0.086,
        "center_motion": 0.202,
        "center_text": 0.38,
        "crop_box": None,
    }

    def _overlap(_path, _start, _dur, label="good", pad_sec=10.0, **_kw):
        return label == "good"

    with patch("pubg_shooting_gate.pubg_probe_segment", return_value=probe), patch(
        "pubg_owner_calibration.pubg_passes_owner_heuristics",
        return_value=(True, "panns_audio=pan0.420:gun0.035"),
    ), patch(
        "pubg_shooting_gate.segment_looks_like_pubg_loot_or_walk", return_value=False
    ), patch(
        "pubg_shooting_gate.segment_is_valid_for_montage",
        return_value=(False, "run_fake_gun=motion0.202:gun0.035"),
    ), patch(
        "pubg_owner_calibration.segment_overlaps_owner_label", side_effect=_overlap
    ):
        ok, reason, metrics = pubg_passes_shooting_gate(
            Path("yt_FxTv16VoLZk.mp4"), 30.8, 51.7, panns_gun_max=0.42
        )
    assert ok is True, (ok, reason, metrics)
    assert metrics.get("owner_style_override") is True
    assert metrics.get("visual_override")
