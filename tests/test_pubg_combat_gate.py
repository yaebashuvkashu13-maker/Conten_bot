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


def test_bot_farm_killfeed_alone_does_not_waive_one_sided() -> None:
    """Kill banners also appear on classic bot kills — require PvP gunfire shape."""
    with patch("pubg_combat_gate._pubg_scan_training_ui", return_value=(False, "")), patch(
        "pubg_combat_gate._pubg_killfeed_hits",
        return_value=("RealSniper eliminated", 1),
    ), patch(
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
    assert reject is True
    assert "bot_farm_one_sided" in reason


def test_bot_farm_passes_with_pvp_shape() -> None:
    with patch("pubg_combat_gate._pubg_scan_training_ui", return_value=(False, "")), patch(
        "pubg_combat_gate._pubg_killfeed_hits",
        return_value=("RealSniper eliminated", 1),
    ), patch(
        "pubg_combat_gate._gunfire_pvp_shape",
        return_value=(3, 3, 0.6),
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


def test_bot_farm_rejects_playerNNNN_victim_name() -> None:
    with patch("pubg_combat_gate._pubg_scan_training_ui", return_value=(False, "")), patch(
        "pubg_combat_gate._pubg_killfeed_hits",
        return_value=("Player1234 eliminated", 1),
    ), patch(
        "pubg_combat_gate._gunfire_pvp_shape",
        return_value=(4, 4, 0.9),
    ), patch("pubg_owner_calibration.segment_overlaps_owner_label", return_value=False):
        reject, reason, row = pubg_rejects_bot_farm(
            Path("x.mp4"),
            100.0,
            10.0,
            gunfire_density=0.08,
            ocr_hits=1,
        )
    assert reject is True
    assert "bot_victim_name" in reason
    assert row.get("bot_victim_name")


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
