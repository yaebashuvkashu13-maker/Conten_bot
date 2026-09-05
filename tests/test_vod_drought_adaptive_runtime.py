"""Behavioral tests: drought floors survive adaptive apply; hard-bad recycle skip."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))


def test_adaptive_apply_respects_drought_gun_floor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("VOD_ADAPTIVE_THRESH_DIR", str(tmp_path / "adaptive"))
    monkeypatch.setenv("VOD_FORCE_SOFTEN", "1")
    monkeypatch.setenv("VOD_FORCE_ESCALATION", "1")
    monkeypatch.setenv("VOD_FORCE_GUN_DENSITY", "0.020")
    monkeypatch.setenv("VOD_FORCE_BURST_RATIO", "3.5")
    from game_adaptive_thresholds import apply_to_environ, thresholds_for

    base = thresholds_for("pubg")
    assert base["gun_density_min"] >= 0.07
    applied = apply_to_environ("pubg")
    assert applied["gun_density_min"] == pytest.approx(0.020)
    assert float(__import__("os").environ["PUBG_SINGLE_MIN_GUN_DENSITY"]) == pytest.approx(0.020)


def test_adaptive_apply_full_floors_without_drought(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("VOD_ADAPTIVE_THRESH_DIR", str(tmp_path / "adaptive"))
    monkeypatch.delenv("VOD_FORCE_SOFTEN", raising=False)
    monkeypatch.delenv("VOD_FORCE_ESCALATION", raising=False)
    monkeypatch.delenv("VOD_FORCE_GUN_DENSITY", raising=False)
    from game_adaptive_thresholds import apply_to_environ, thresholds_for

    base = thresholds_for("pubg")
    applied = apply_to_environ("pubg")
    assert applied["gun_density_min"] == pytest.approx(base["gun_density_min"])


def test_force_send_drought_esc0_keeps_loot_reject() -> None:
    from vod_force_send import apply_drought_pubg_env

    env = apply_drought_pubg_env({}, escalation=0)
    assert env["PUBG_PRESEND_SHOOTING_GATE"] == "1"
    assert env["PUBG_REJECT_LOOT_WALK"] == "1"
    assert env["VOD_FORCE_SOFTEN"] == "1"
    assert env["VOD_FORCE_GUN_DENSITY"] == env["PUBG_SINGLE_MIN_GUN_DENSITY"]
    assert env.get("VOD_FORCE_PRESEND_BYPASS", "0") != "1"


def test_force_send_drought_esc2_caps_owner_relax() -> None:
    from vod_force_send import apply_drought_pubg_env

    env = apply_drought_pubg_env({}, escalation=2)
    assert env["PUBG_PRESEND_SHOOTING_GATE"] == "1"
    assert env["VOD_FORCE_PRESEND_BYPASS"] == "0"
    assert env["PUBG_RELAX_OWNER_HEURISTICS"] == "1"
    assert float(env["PUBG_SINGLE_MIN_GUN_DENSITY"]) <= 0.0101


def test_hang_recover_esc0_keeps_loot_reject(monkeypatch: pytest.MonkeyPatch) -> None:
    from vod_hang_detector import apply_agent_recover_env

    monkeypatch.setattr("vod_hang_detector.last_send_age_sec", lambda: 9000.0)
    out = apply_agent_recover_env({}, escalation=0)
    assert out["VOD_FORCE_SOFTEN"] == "1"
    assert out["PUBG_REJECT_LOOT_WALK"] == "1"
    assert out["VOD_FORCE_REJECT_LOOT"] == "1"
    assert out["VOD_FORCE_PRESEND_BYPASS"] == "0"
    assert out.get("PUBG_SINGLES_GUN_PAYOFF_BYPASS") == "1"
    assert out.get("PUBG_SINGLES_GUN_QUALITY_BYPASS") == "1"


def test_soften_hard_assigns_over_stale_strict_pins(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pinned VOD_FORCE_* must not make drought stricter than steady state."""
    from vod_force_send import apply_drought_pubg_env
    from vod_hang_detector import apply_agent_recover_env

    monkeypatch.setattr("vod_hang_detector.last_send_age_sec", lambda: 9000.0)
    stale = {
        "VOD_FORCE_QUALITY_MIN": "0.40",
        "VOD_FORCE_PAYOFF_MIN": "0.30",
        "VOD_FORCE_GUN_DENSITY": "0.08",
    }
    force = apply_drought_pubg_env(dict(stale), escalation=1)
    hang = apply_agent_recover_env(dict(stale), escalation=1)
    assert float(force["VOD_FORCE_QUALITY_MIN"]) == pytest.approx(0.24)
    assert float(force["VOD_FORCE_PAYOFF_MIN"]) == pytest.approx(0.05)
    assert float(hang["VOD_FORCE_QUALITY_MIN"]) == pytest.approx(0.24)
    assert float(hang["VOD_FORCE_PAYOFF_MIN"]) == pytest.approx(0.05)
    assert float(force["PUBG_FAST_PAYOFF_MIN"]) == pytest.approx(0.05)
    assert float(hang["PUBG_FAST_PAYOFF_MIN"]) == pytest.approx(0.05)
    assert float(force["PUBG_PRESEND_MIN_GUN_DENSITY"]) == pytest.approx(0.020)
    assert float(hang["PUBG_PRESEND_MIN_GUN_DENSITY"]) == pytest.approx(0.020)


def test_hang_recover_esc2_caps_owner_relax(monkeypatch: pytest.MonkeyPatch) -> None:
    from vod_hang_detector import apply_agent_recover_env

    monkeypatch.setattr("vod_hang_detector.last_send_age_sec", lambda: 9000.0)
    out = apply_agent_recover_env({}, escalation=2)
    assert out["PUBG_RELAX_OWNER_HEURISTICS"] == "1"
    assert out["PUBG_PRESEND_SHOOTING_GATE"] != "0"
    assert out["VOD_FORCE_PRESEND_BYPASS"] == "0"


def test_soften_relaxes_hook_menu_not_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drought must soften HUD false-positives without disabling hook gate."""
    from vod_force_send import apply_drought_pubg_env
    from vod_hang_detector import apply_agent_recover_env

    monkeypatch.setattr("vod_hang_detector.last_send_age_sec", lambda: 9000.0)
    force = apply_drought_pubg_env({}, escalation=2)
    hang = apply_agent_recover_env({}, escalation=2)
    for env in (force, hang):
        assert env["CLIP_HOOK_GATE"] == "1"
        assert float(env["CLIP_HOOK_MAX_MENU"]) == pytest.approx(0.78)
        assert float(env["CLIP_HOOK_MIN_AUDIO_RMS"]) == pytest.approx(0.03)
        assert float(env["DISLIKE_MENU_OVERLAY_MAX"]) == pytest.approx(0.30)
        assert env["PUBG_HARD_REJECT_MENU_OVERLAY"] == "1"
        assert float(env["VOD_FORCE_QUALITY_MIN"]) == pytest.approx(0.20)
        assert env["PUBG_COMBAT_TIMELINE"] == "1"
        assert env["PUBG_REJECT_LOOT_WALK"] == "1"
        assert env["PUBG_PRESEND_SHOOTING_GATE"] == "1"


def test_entry_hard_bad_without_peaks() -> None:
    from vod_inbox_recover import entry_is_hard_bad_without_peaks

    assert entry_is_hard_bad_without_peaks(
        {"reject_reason": "hard_loot_walk", "peaks": []}
    )
    assert not entry_is_hard_bad_without_peaks(
        {"reject_reason": "hard_loot_walk", "peaks": [12.0, 40.0]}
    )
    assert not entry_is_hard_bad_without_peaks(
        {"reject_reason": "no_sendable_peaks", "peaks": []}
    )


def test_recycle_skips_hard_bad_source() -> None:
    src = (SCRIPTS / "shooter_vod_segment_feed.py").read_text(encoding="utf-8")
    assert "entry_is_hard_bad_without_peaks" in src
    assert "Same sticky menu/loot skip as inbox recover" in src
