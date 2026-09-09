"""Global fight-act profile must apply without per-VOD owner labels."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))


def test_is_combat_act_matches_owner_sprays() -> None:
    from pubg_fight_act_profile import is_combat_act

    # Typical owner 6mW act: mid gun + strong burst
    assert is_combat_act(0.040, 9.0) is True
    assert is_combat_act(0.068, 6.0) is True
    # Silent loot / talk
    assert is_combat_act(0.010, 2.0) is False
    assert is_combat_act(0.025, 3.0) is False


def test_apply_global_act_defaults_sets_floors(monkeypatch: pytest.MonkeyPatch) -> None:
    from pubg_fight_act_profile import ACT_MIN_GUN, apply_global_act_defaults

    for key in (
        "PUBG_SINGLE_MIN_GUN_DENSITY",
        "PUBG_CLIP_MIN_BURST_RATIO",
        "PUBG_COMBAT_ACT_PAYOFF_BYPASS",
        "PUBG_STYLE_USE_GLOBAL_ACT_PROFILE",
    ):
        monkeypatch.delenv(key, raising=False)
    apply_global_act_defaults()
    assert float(__import__("os").environ["PUBG_SINGLE_MIN_GUN_DENSITY"]) == ACT_MIN_GUN
    assert __import__("os").environ["PUBG_COMBAT_ACT_PAYOFF_BYPASS"] == "1"
    assert __import__("os").environ["PUBG_STYLE_USE_GLOBAL_ACT_PROFILE"] == "1"


def test_style_similarity_does_not_punish_ocr_blind_global_profile() -> None:
    from pubg_fight_act_profile import GLOBAL_ACT_STYLE_PROFILE
    from pubg_owner_style import style_similarity

    row = {
        "gunfire_density": 0.065,
        "panns_gun_max": 0.40,
        "notification_score": 0.05,
        "payoff_fast": 0.02,  # OCR-blind
        "fight_fast": 0.55,
        "notification_hit": False,
        "loot_walk": False,
    }
    sim = style_similarity(GLOBAL_ACT_STYLE_PROFILE, row)
    # Must not collapse just because payoff OCR is empty.
    assert sim >= 0.30


def test_drought_baseline_uses_act_floors() -> None:
    from pubg_drought_elasticity import DEFAULT_BASELINE

    assert DEFAULT_BASELINE["PUBG_SINGLE_MIN_GUN_DENSITY"] <= 0.035
    assert DEFAULT_BASELINE["PUBG_PAYOFF_SCORE_MIN_SINGLES"] <= 0.12


def test_mid_burst_owner_reject_shape_is_combat_act() -> None:
    """Wg9-style rejects: gun≈0.05 burst≈3.8 must pass as combat acts."""
    from pubg_fight_act_profile import is_combat_act
    from pubg_owner_calibration import pubg_passes_owner_heuristics

    assert is_combat_act(0.057, 3.78) is True
    assert is_combat_act(0.039, 4.65) is True
    assert is_combat_act(0.033, 3.73) is True
    ok, reason = pubg_passes_owner_heuristics(
        0.057, 3.78, 0.05, 0.127, panns_gun_max=0.51
    )
    assert ok, reason
    assert "combat_act" in reason or "metro_act" in reason or "panns" in reason


def test_forbidden_fake_gun_rescued_by_combat_act(monkeypatch) -> None:
    """Low-PANNs run_fake_gun with act-shaped audio must not hard-stop."""
    import pubg_shooting_gate as gate

    monkeypatch.setenv("PUBG_GLOBAL_FIGHT_ACT", "1")
    monkeypatch.setenv("PUBG_FIGHT_ACT_MIN_BURST", "3.5")
    # Simulate the early path via heuristics alone.
    from pubg_owner_calibration import pubg_passes_owner_heuristics

    ok, reason = pubg_passes_owner_heuristics(
        0.066, 5.06, 0.04, 0.15, panns_gun_max=0.25
    )
    assert ok, reason
