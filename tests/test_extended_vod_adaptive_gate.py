"""Tests for extended VOD adaptive gate (Genshin / WoT)."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

from extended_vod_adaptive_gate import (  # noqa: E402
    adaptive_env,
    overrides_for_level,
    soften_level,
)


def test_genshin_soften_levels(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXTENDED_VOD_ZERO_STREAK_SOFTEN", "2")
    assert soften_level(1) == 0
    assert soften_level(2) == 1
    assert soften_level(3) == 2
    assert soften_level(6) == 3


def test_genshin_adaptive_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXTENDED_VOD_ZERO_STREAK_SOFTEN", "2")
    monkeypatch.delenv("SMART_GENSHIN_STRICT_MIN_BOSS_SCORE", raising=False)
    with adaptive_env("genshin", 2) as level:
        assert level == 1
        assert os.environ["SMART_GENSHIN_STRICT_MIN_BOSS_SCORE"] == overrides_for_level("genshin", 1)[
            "SMART_GENSHIN_STRICT_MIN_BOSS_SCORE"
        ]


def test_wot_l3_relaxes_impact() -> None:
    ov = overrides_for_level("wot", 3)
    assert float(ov["SMART_WOT_MIN_IMPACT_DENSITY"]) < float(
        overrides_for_level("wot", 1)["SMART_WOT_MIN_IMPACT_DENSITY"]
    )
    assert float(ov["SMART_WOT_CRUISE_IMPACT_CAP"]) < float(
        overrides_for_level("wot", 1)["SMART_WOT_CRUISE_IMPACT_CAP"]
    )
    # Soften must stay below typical production defaults (never harden a dry streak).
    assert float(ov["SMART_WOT_MIN_IMPACT_DENSITY"]) <= 0.012
    assert float(ov["SMART_WOT_CRUISE_IMPACT_CAP"]) <= 0.025


def test_wot_cruise_cap_env(monkeypatch: pytest.MonkeyPatch) -> None:
    from strict_segment_gate import _wot_extra_reject

    monkeypatch.setenv("WOT_BRAWL_GATE", "1")
    monkeypatch.setenv("SMART_WOT_MIN_IMPACT_DENSITY", "0.012")
    monkeypatch.setenv("WOT_BRAWL_MIN_HIT_FLASHES", "1")
    monkeypatch.setenv("SMART_WOT_CRUISE_IMPACT_CAP", "0.020")
    # High motion, impact at/above lowered cruise cap — passes.
    reject, reason = _wot_extra_reject(
        {"impact_density": 0.022, "center_motion": 0.18, "hit_flash_count": 2, "burst_ratio": 3.0}
    )
    assert reject is False, reason
    # Same clip fails with old-style high cruise cap.
    monkeypatch.setenv("SMART_WOT_CRUISE_IMPACT_CAP", "0.050")
    reject, reason = _wot_extra_reject(
        {"impact_density": 0.022, "center_motion": 0.18, "hit_flash_count": 2, "burst_ratio": 3.0}
    )
    assert reject is True
    assert "cruise_no_action" in reason