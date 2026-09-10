from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))


def test_combat_act_rescues_borderline_menu_overlay(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PUBG_GLOBAL_FIGHT_ACT", "1")
    monkeypatch.setenv("PUBG_FIGHT_ACT_MIN_BURST", "3.5")
    monkeypatch.setenv("PUBG_DISLIKE_COMBAT_ACT_MENU_RESCUE", "1")
    from dislike_reason_gates import evaluate_reason_gates

    ok, reason, report = evaluate_reason_gates(
        {
            "gun_density": 0.055,
            "burst_ratio": 4.0,
            "center_motion": 0.12,
            "menu_overlay": 0.30,
        },
        active_reasons=["menu"],
    )
    assert ok, reason
    assert report.get("combat_act_menu_rescue") is True


def test_true_menu_without_combat_still_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PUBG_GLOBAL_FIGHT_ACT", "1")
    monkeypatch.setenv("PUBG_DISLIKE_COMBAT_ACT_MENU_RESCUE", "1")
    from dislike_reason_gates import evaluate_reason_gates

    ok, reason, report = evaluate_reason_gates(
        {
            "gun_density": 0.010,
            "burst_ratio": 1.5,
            "center_motion": 0.05,
            "menu_overlay": 0.30,
        },
        active_reasons=["menu"],
    )
    assert not ok
    assert "menu_overlay" in reason


def test_combat_act_does_not_rescue_loot_run_floors(monkeypatch: pytest.MonkeyPatch) -> None:
    """vhTD-style mid-burst under loot_run must stay rejected (owner 👎 loot/no_kill)."""
    monkeypatch.setenv("PUBG_GLOBAL_FIGHT_ACT", "1")
    monkeypatch.setenv("PUBG_FIGHT_ACT_MIN_BURST", "3.5")
    monkeypatch.setenv("PUBG_DISLIKE_COMBAT_ACT_RESCUE", "1")
    monkeypatch.setenv("PUBG_DISLIKE_LOOT_FLOOR_LOCK", "1")
    monkeypatch.setenv("DISLIKE_GUN_DENSITY_MIN", "0.015")
    monkeypatch.setenv("DISLIKE_BURST_RATIO_MIN", "3.0")
    from dislike_reason_gates import evaluate_reason_gates

    ok, reason, report = evaluate_reason_gates(
        {
            "gun_density": 0.0567,
            "burst_ratio": 5.295,
            "center_motion": 0.126,
            "menu_overlay": 0.297,
        },
        active_reasons=["menu", "loot_run", "loot_run"],
    )
    assert not ok, reason
    assert "loot_run" in reason or "low_burst" in reason or "low_gun" in reason
    assert report.get("combat_act_rescue") is not True
    assert "loot_run" in (report.get("combat_act_rescue_blocked_by") or [])
    # Drought soften must not undercut loot floors.
    assert report["floors"]["burst_ratio_min"] >= 7.5
    assert report["floors"]["gun_density_min"] >= 0.090


def test_no_kill_reason_requires_kill_evidence(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PUBG_DISLIKE_REQUIRE_KILL_EVIDENCE", "1")
    monkeypatch.setenv("PUBG_DISLIKE_COMBAT_ACT_RESCUE", "0")
    from dislike_reason_gates import evaluate_reason_gates

    ok, reason, _report = evaluate_reason_gates(
        {
            "gun_density": 0.076,
            "burst_ratio": 9.4,
            "center_motion": 0.10,
            "menu_overlay": 0.05,
            "has_author_kill": False,
            "kill_notification_hit": False,
            "killfeed_density": 0.0,
            "kill_notification_score": 0.0,
        },
        active_reasons=["no_kill"],
    )
    assert not ok
    assert "no_kill" in reason

    ok2, reason2, _ = evaluate_reason_gates(
        {
            "gun_density": 0.076,
            "burst_ratio": 9.4,
            "center_motion": 0.10,
            "menu_overlay": 0.05,
            "has_author_kill": True,
            "kill_notification_hit": True,
            "killfeed_density": 0.5,
            "kill_notification_score": 0.7,
        },
        active_reasons=["no_kill"],
    )
    assert ok2, reason2


def test_fight_candidate_waives_no_kill_dislike_floors(monkeypatch: pytest.MonkeyPatch) -> None:
    """OCR-blind fight-candidate must not be re-blocked by recent 👎 no_kill floors."""
    monkeypatch.setenv("PUBG_GLOBAL_FIGHT_ACT", "1")
    monkeypatch.setenv("PUBG_FIGHT_ACT_MIN_GUN", "0.032")
    monkeypatch.setenv("PUBG_FIGHT_ACT_MIN_BURST", "3.5")
    monkeypatch.setenv("PUBG_DISLIKE_COMBAT_ACT_RESCUE", "1")
    monkeypatch.setenv("PUBG_DISLIKE_REQUIRE_KILL_EVIDENCE", "1")
    monkeypatch.setenv("PUBG_FIGHT_CANDIDATE_DISLIKE_RESCUE", "1")
    monkeypatch.setenv("DISLIKE_GUN_DENSITY_MIN", "0.090")
    monkeypatch.setenv("DISLIKE_BURST_RATIO_MIN", "4.5")
    from dislike_reason_gates import evaluate_reason_gates

    ok, reason, report = evaluate_reason_gates(
        {
            "gun_density": 0.066,
            "burst_ratio": 4.4,
            "center_motion": 0.08,
            "menu_overlay": 0.10,
            "has_author_kill": False,
            "kill_notification_hit": False,
            "killfeed_density": 0.0,
            "kill_notification_score": 0.0,
            "fight_candidate_owner_review": True,
        },
        active_reasons=["no_kill", "low_gun"],
    )
    assert ok, reason
    assert report.get("fight_candidate_dislike_rescue") is True
    assert report.get("fight_candidate_no_kill_evidence_waive") is True
    assert report["floors"]["gun_density_min"] <= 0.032

    # Without the fight-candidate flag, recent no_kill still hard-blocks.
    ok2, reason2, report2 = evaluate_reason_gates(
        {
            "gun_density": 0.066,
            "burst_ratio": 4.4,
            "center_motion": 0.08,
            "menu_overlay": 0.10,
            "has_author_kill": False,
            "kill_notification_hit": False,
            "killfeed_density": 0.0,
            "kill_notification_score": 0.0,
        },
        active_reasons=["no_kill", "low_gun"],
    )
    assert not ok2
    assert "no_kill" in reason2 or "low_gun" in reason2 or "low_burst" in reason2
    assert "no_kill" in (report2.get("combat_act_rescue_blocked_by") or [])
