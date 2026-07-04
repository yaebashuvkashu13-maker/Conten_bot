from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

from pubg_combat_gate import pubg_passes_combat_gate, pubg_rejects_bot_farm  # noqa: E402


def _combat_gate_patches():
    return (
        patch(
            "pubg_combat_gate.pubg_passes_shooting_gate",
            return_value=(
                True,
                "strict_gun",
                {"gunfire_density": 0.08, "burst_ratio": 5.0, "crop_box": None, "center_motion": 0.04},
            ),
        ),
        patch(
            "highlight_scorer.score_panns_audio",
            return_value={"panns_gun_max": 0.30},
        ),
        patch(
            "highlight_scorer.calibrated_pann_gun_min",
            return_value=0.18,
        ),
        patch(
            "pubg_combat_gate.pubg_combat_visual_strict",
            return_value=(True, "combat_visual_strict", {}),
        ),
        patch(
            "pubg_combat_gate.segment_looks_like_pubg_loot_or_walk",
            return_value=False,
        ),
    )


def test_combat_gate_rejects_low_panns() -> None:
    with patch(
        "pubg_combat_gate.pubg_passes_shooting_gate",
        return_value=(True, "strict_gun", {"gunfire_density": 0.08, "burst_ratio": 5.0, "crop_box": None}),
    ), patch(
        "highlight_scorer.score_panns_audio",
        return_value={"panns_gun_max": 0.10},
    ), patch(
        "highlight_scorer.calibrated_pann_gun_min",
        return_value=0.18,
    ), patch(
        "pubg_combat_gate.pubg_combat_visual_strict",
        return_value=(True, "combat_visual_strict", {}),
    ), patch(
        "pubg_combat_gate.segment_looks_like_pubg_loot_or_walk",
        return_value=False,
    ):
        ok, reason, _ = pubg_passes_combat_gate(Path("x.mp4"), 100.0, 10.0, "pubg")
    assert ok is False
    assert "panns_gun_low" in reason


def test_bot_farm_rejects_one_sided_gunfire() -> None:
    with patch("pubg_combat_gate._pubg_scan_training_ui", return_value=(False, "")), patch(
        "pubg_combat_gate._pubg_killfeed_hits",
        return_value=("", 0),
    ), patch(
        "pubg_combat_gate._gunfire_pvp_shape",
        return_value=(1, 1, 0.1),
    ), patch("pubg_owner_calibration.segment_overlaps_owner_label", return_value=False):
        reject, reason, _ = pubg_rejects_bot_farm(
            Path("x.mp4"),
            100.0,
            10.0,
            gunfire_density=0.08,
            center_motion=0.02,
            minimap_delta=0.003,
            ocr_hits=0,
        )
    assert reject is True
    assert "bot_farm_one_sided" in reason


def test_bot_farm_passes_with_killfeed() -> None:
    with patch("pubg_combat_gate._pubg_scan_training_ui", return_value=(False, "")), patch(
        "pubg_combat_gate._gunfire_pvp_shape",
        return_value=(1, 1, 0.1),
    ), patch("pubg_owner_calibration.segment_overlaps_owner_label", return_value=False):
        reject, reason, _ = pubg_rejects_bot_farm(
            Path("x.mp4"),
            100.0,
            10.0,
            gunfire_density=0.08,
            ocr_hits=2,
        )
    assert reject is False
    assert reason == ""


def test_combat_gate_rejects_bot_farm_for_pubg() -> None:
    patches = _combat_gate_patches()
    with patches[0], patches[1], patches[2], patches[3], patches[4], patch(
        "pubg_combat_gate.pubg_rejects_bot_farm",
        return_value=(True, "bot_farm_one_sided=quarters1:clusters1:kf0:mini0.003", {"killfeed_hits": 0}),
    ):
        ok, reason, row = pubg_passes_combat_gate(Path("x.mp4"), 100.0, 10.0, "pubg")
    assert ok is False
    assert "bot_farm" in reason
    assert "bot_farm" in row


def test_combat_gate_scan_fast_panns_trust_when_visual_misses() -> None:
    with patch(
        "highlight_scorer.score_panns_audio",
        return_value={"panns_gun_max": 0.52},
    ), patch(
        "highlight_scorer.calibrated_pann_gun_min",
        return_value=0.25,
    ), patch(
        "pubg_combat_gate.pubg_combat_visual_fast",
        return_value=(False, "no_combat_signal flash=0.0000 weapon=0.0000", {}),
    ), patch(
        "pubg_combat_gate.pubg_passes_shooting_gate",
        return_value=(True, "strict_gun", {"gunfire_density": 0.06, "burst_ratio": 4.5}),
    ), patch(
        "pubg_combat_gate.pubg_passes_shooting_gate",
        return_value=(True, "strict_gun", {"gunfire_density": 0.06, "burst_ratio": 4.5}),
    ):
        ok, reason, row = pubg_passes_combat_gate(
            Path("x.mp4"), 100.0, 10.0, "pubg", scan_fast=True
        )
    assert ok is True
    assert row.get("panns_visual_trust") is True
