from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

from pubg_combat_gate import pubg_passes_combat_gate  # noqa: E402


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
