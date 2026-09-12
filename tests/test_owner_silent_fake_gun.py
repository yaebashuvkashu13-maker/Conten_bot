from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))


def test_combat_act_rejects_silent_dsp_false_gun(monkeypatch: pytest.MonkeyPatch) -> None:
    """Owner 👎 DGso 257/1026: gun+burst without audible RMS must not pass."""
    monkeypatch.setenv("PUBG_OWNER_FIGHT_MIN_RMS", "0.020")
    monkeypatch.setenv("PUBG_GLOBAL_FIGHT_ACT", "1")
    from pubg_owner_calibration import pubg_passes_owner_heuristics

    ok, reason = pubg_passes_owner_heuristics(
        gunfire_density=0.089,
        burst_ratio=5.33,
        audio_rms=0.0085,
        center_motion=0.102,
    )
    assert ok is False
    assert "silent_fake_gun" in reason


def test_combat_act_allows_audible_fight(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PUBG_OWNER_FIGHT_MIN_RMS", "0.020")
    monkeypatch.setenv("PUBG_GLOBAL_FIGHT_ACT", "1")
    from pubg_owner_calibration import pubg_passes_owner_heuristics

    ok, reason = pubg_passes_owner_heuristics(
        gunfire_density=0.058,
        burst_ratio=4.68,
        audio_rms=0.075,
        center_motion=0.161,
    )
    assert ok is True
    assert "combat_act" in reason
