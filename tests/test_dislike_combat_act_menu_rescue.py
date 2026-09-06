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
