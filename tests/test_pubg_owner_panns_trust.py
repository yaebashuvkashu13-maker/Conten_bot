"""PUBG owner heuristics must trust PANNs when RMS gunfire detector lags."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from pubg_owner_calibration import pubg_passes_owner_heuristics  # noqa: E402


def test_panns_trust_overrides_talk_menu(monkeypatch) -> None:
    monkeypatch.setenv("PUBG_PANNS_TRUST_MIN", "0.35")
    ok, reason = pubg_passes_owner_heuristics(
        0.0,
        0.0,
        0.26,
        0.18,
        panns_gun_max=0.52,
    )
    assert ok is True
    assert reason.startswith("panns_trust=")


def test_talk_menu_when_panns_weak(monkeypatch) -> None:
    monkeypatch.setenv("PUBG_PANNS_TRUST_MIN", "0.35")
    ok, reason = pubg_passes_owner_heuristics(
        0.0,
        0.0,
        0.26,
        0.18,
        panns_gun_max=0.10,
    )
    assert ok is False
    assert reason.startswith("talk_menu=")
