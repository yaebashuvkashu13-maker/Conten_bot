
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from pubg_owner_calibration import pubg_passes_owner_heuristics


def test_panns_trust_rejects_fake_gun_loot_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """High PANNs alone must not pass inventory/loot runs (_-HbZ0zNDOs_2538)."""
    monkeypatch.setenv("PUBG_PANNS_TRUST_MIN", "0.35")
    ok, reason = pubg_passes_owner_heuristics(
        gunfire_density=0.056,
        burst_ratio=5.227,
        audio_rms=0.031,
        center_motion=0.201,
        panns_gun_max=0.740,
    )
    assert ok is False
    assert reason.startswith("run_fake_gun")


def test_panns_trust_still_passes_real_fight(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PUBG_PANNS_TRUST_MIN", "0.35")
    ok, reason = pubg_passes_owner_heuristics(
        gunfire_density=0.090,
        burst_ratio=8.0,
        audio_rms=0.04,
        center_motion=0.04,
        panns_gun_max=0.70,
    )
    assert ok is True
    assert reason.startswith("panns_trust")
