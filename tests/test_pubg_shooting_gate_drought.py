
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))


def test_min_gunfire_respects_drought_soften(monkeypatch):
    monkeypatch.setenv("VOD_FORCE_SOFTEN", "1")
    monkeypatch.setenv("SMART_PUBG_MIN_GUNFIRE_DENSITY", "0.010")
    from pubg_shooting_gate import _min_gunfire

    assert _min_gunfire() == 0.010


def test_min_gunfire_keeps_quality_floor_steady(monkeypatch):
    monkeypatch.delenv("VOD_FORCE_SOFTEN", raising=False)
    monkeypatch.delenv("VOD_FORCE_ESCALATION", raising=False)
    monkeypatch.setenv("SMART_PUBG_MIN_GUNFIRE_DENSITY", "0.010")
    from pubg_shooting_gate import QUALITY_FLOOR_GUNFIRE, _min_gunfire

    assert _min_gunfire() == QUALITY_FLOOR_GUNFIRE
