"""Hero fight vs creep farm gate."""

from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import mlbb_fight_structure as fight_struct  # noqa: E402


def test_rejects_creep_farm_shape(monkeypatch) -> None:
    monkeypatch.setenv("MLBB_HERO_FIGHT_GATE", "1")
    creep = [(0.035, 0.004, 0.003)] * 5
    monkeypatch.setattr(
        fight_struct,
        "analyze_fight_timeline",
        lambda *a, **k: {
            "bins": [{"motion": 0.035, "mini": 0.004, "skill": 0.003, "combat": 0.1}] * 5,
            "peak_idx": 2,
            "hero_bins": 0,
            "creep_bins": 5,
            "avg_motion": 0.035,
            "avg_mini": 0.004,
            "avg_skill": 0.003,
            "baseline_combat": 0.1,
            "peak_combat": 0.11,
            "peak_lift": 1.05,
            "hud_activity": 0.007,
        },
    )
    ok, reason, _ = fight_struct.passes_hero_fight_gate(
        Path("/tmp/v.mp4"), 10.0, 18.0, peak_start=14.0
    )
    assert not ok
    assert "creep" in reason


def test_accepts_teamfight_shape(monkeypatch) -> None:
    monkeypatch.setenv("MLBB_HERO_FIGHT_GATE", "1")
    monkeypatch.setattr(
        fight_struct,
        "analyze_fight_timeline",
        lambda *a, **k: {
            "bins": [
                {"motion": 0.03, "mini": 0.006, "skill": 0.005, "combat": 0.12},
                {"motion": 0.04, "mini": 0.014, "skill": 0.012, "combat": 0.22},
                {"motion": 0.05, "mini": 0.018, "skill": 0.016, "combat": 0.28},
                {"motion": 0.04, "mini": 0.013, "skill": 0.011, "combat": 0.20},
                {"motion": 0.03, "mini": 0.008, "skill": 0.007, "combat": 0.14},
            ],
            "peak_idx": 2,
            "hero_bins": 4,
            "creep_bins": 0,
            "avg_motion": 0.038,
            "avg_mini": 0.012,
            "avg_skill": 0.010,
            "baseline_combat": 0.19,
            "peak_combat": 0.28,
            "peak_lift": 1.47,
            "hud_activity": 0.022,
        },
    )
    ok, reason, _ = fight_struct.passes_hero_fight_gate(
        Path("/tmp/v.mp4"), 120.0, 20.0, peak_start=128.0, multikill=True
    )
    assert ok
    assert reason == "hero_fight_ok"
