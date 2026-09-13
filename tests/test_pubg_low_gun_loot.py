from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))


def test_shooting_gate_rejects_dgso775_silent_low_panns_loot(monkeypatch: pytest.MonkeyPatch) -> None:
    """Owner 👎 DGsoEt9znls_775: loot without run, almost no gunfire."""
    monkeypatch.setenv("PUBG_OWNER_FIGHT_MIN_RMS", "0.020")
    monkeypatch.setenv("PUBG_GLOBAL_FIGHT_ACT", "1")
    monkeypatch.setenv("PUBG_SHOOTING_REQUIRE_COMBAT_ACT_RMS", "1")
    monkeypatch.setenv("PUBG_SHOOTING_REQUIRE_COMBAT_ACT_PANNS", "1")
    monkeypatch.setenv("PUBG_COMBAT_ACT_MIN_PANNS", "0.10")
    monkeypatch.setenv("PUBG_REJECT_LOW_GUN_LOOT", "1")
    monkeypatch.setenv("PUBG_LOW_GUN_LOOT_MAX_GUN", "0.050")
    monkeypatch.setenv("PUBG_LOW_GUN_LOOT_MAX_PANNS", "0.12")
    monkeypatch.setenv("PUBG_LOW_GUN_LOOT_SHORT_SEC", "10")
    monkeypatch.setenv("SMART_PUBG_MIN_GUNFIRE_DENSITY", "0.038")
    monkeypatch.setenv("VOD_FORCE_SOFTEN", "0")
    monkeypatch.setenv("SHOOTER_VOD_SOFTEN_LEVEL", "0")

    from pubg_shooting_gate import pubg_passes_shooting_gate

    metrics = {
        "start": 775.5,
        "duration": 6.0,
        "gunfire_density": 0.0428,
        "burst_ratio": 10.24,
        "audio_rms": 0.0035,
        "center_motion": 0.0821,
        "center_text": 0.10,
        "crop_box": [0, 102, 1920, 876],
    }

    with (
        patch("pubg_shooting_gate.pubg_probe_segment", return_value=dict(metrics)),
        patch(
            "pubg_shooting_gate.segment_looks_like_pubg_loot_or_walk",
            return_value=False,
        ),
        patch(
            "pubg_shooting_gate.segment_is_valid_for_montage",
            return_value=(False, "silent_fake_gun=rms0.0035:gun0.043"),
        ),
    ):
        ok, reason, out = pubg_passes_shooting_gate(
            Path("/tmp/fake.mp4"),
            775.5,
            6.0,
            panns_gun_max=0.026,
        )
    assert ok is False
    assert "low_gun_loot" in reason or "silent_fake_gun" in reason
    assert out.get("combat_act_override") is not True


def test_loot_or_walk_flags_soft_motion_weak_gun(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SMART_PUBG_MIN_GUNFIRE_DENSITY", "0.038")
    monkeypatch.setenv("SMART_PUBG_LOOT_ESCAPE_GUN", "0.055")
    monkeypatch.setenv("SMART_PUBG_LOOT_SOFT_MOTION", "0.055")
    monkeypatch.setenv("SMART_PUBG_LOOT_SOFT_GUN", "0.050")
    from gameplay_gate import segment_looks_like_pubg_loot_or_walk

    with patch(
        "gameplay_gate.score_segment_combat",
        return_value=(0.0821, 0.0, 0.0, 0.309),
    ):
        assert (
            segment_looks_like_pubg_loot_or_walk(
                Path("/tmp/fake.mp4"),
                775.5,
                6.0,
                gunfire_density=0.0428,
                burst_ratio=10.24,
            )
            is True
        )


def test_dislike_rejects_loot_low_gun_without_run(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PUBG_DISLIKE_LOOT_LOW_GUN_GATE", "1")
    monkeypatch.setenv("PUBG_DISLIKE_LOOT_LOW_GUN", "0.050")
    monkeypatch.setenv("PUBG_DISLIKE_LOOT_LOW_PANNS", "0.12")
    monkeypatch.setenv("PUBG_OWNER_FIGHT_MIN_RMS", "0.020")
    monkeypatch.setenv("PUBG_DISLIKE_LOOT_FLOOR_LOCK", "1")
    from dislike_reason_gates import evaluate_reason_gates

    ok, reason, _report = evaluate_reason_gates(
        {
            "gunfire_density": 0.0428,
            "burst_ratio": 10.24,
            "center_motion": 0.0821,
            "center_text": 0.10,
            "audio_rms": 0.0035,
            "panns_gun_max": 0.026,
            "has_author_kill": False,
            "kill_notification_hit": False,
        },
        active_reasons=["menu", "loot_run", "no_kill"],
    )
    assert ok is False
    assert any(x in reason for x in ("loot_low_gun", "low_gun", "menu_overlay"))
