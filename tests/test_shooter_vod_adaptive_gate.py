"""Tests for shooter VOD adaptive gate."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

from shooter_vod_adaptive_gate import (  # noqa: E402
    adaptive_env,
    overrides_for_level,
    record_vod_outcome,
    soften_level,
    streak_from_state,
)


def test_soften_capped_at_quality_max(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SHOOTER_VOD_ZERO_STREAK_SOFTEN", "2")
    monkeypatch.delenv("SHOOTER_VOD_MAX_SOFTEN_LEVEL", raising=False)  # default 1
    monkeypatch.delenv("SHOOTER_VOD_DISABLE_SOFTEN", raising=False)
    assert soften_level(0) == 0
    assert soften_level(1) == 0
    assert soften_level(2) == 1
    assert soften_level(10) == 1  # capped — no L3/L4 trash path


def test_soften_can_raise_ceiling_explicitly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SHOOTER_VOD_ZERO_STREAK_SOFTEN", "2")
    monkeypatch.setenv("SHOOTER_VOD_MAX_SOFTEN_LEVEL", "4")
    assert soften_level(2) == 1
    assert soften_level(3) == 2
    assert soften_level(6) == 3
    assert soften_level(10) == 4


def test_adaptive_env_applies_menu_relax(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SHOOTER_VOD_ZERO_STREAK_SOFTEN", "2")
    monkeypatch.delenv("VISUAL_MENU_OVERLAY_MAX", raising=False)
    with adaptive_env(2) as level:
        assert level == 1
        assert os.environ["VISUAL_MENU_OVERLAY_MAX"] == "0.58"
    assert "VISUAL_MENU_OVERLAY_MAX" not in os.environ or os.environ.get("VISUAL_MENU_OVERLAY_MAX") != "0.58"


def test_record_vod_outcome_streak() -> None:
    state: dict = {}
    record_vod_outcome(state, vod_id="a", sent=0)
    record_vod_outcome(state, vod_id="b", sent=0)
    assert streak_from_state(state) == 2
    record_vod_outcome(state, vod_id="c", sent=1)
    assert streak_from_state(state) == 0


def test_l2_has_pov_gate_off() -> None:
    ov = overrides_for_level(2)
    assert ov.get("PUBG_POV_GATE") == "0"
    assert float(ov["VISUAL_MENU_OVERLAY_MAX"]) > float(overrides_for_level(1)["VISUAL_MENU_OVERLAY_MAX"])
    assert ov.get("PUBG_METRO_VOD_MIN_PROBES") == "1"
    assert ov.get("PUBG_METRO_SEGMENT_RELAX") == "1"
    assert ov.get("PUBG_RELAX_OWNER_HEURISTICS") == "0"


def test_l3_never_enables_relax_heuristics() -> None:
    ov = overrides_for_level(3)
    assert ov.get("PUBG_METRO_SEGMENT_TRUST_VOD") == "1"
    assert ov.get("PUBG_RELAX_OWNER_HEURISTICS") == "0"


def test_l4_never_enables_relax_heuristics() -> None:
    ov = overrides_for_level(4)
    assert ov.get("PUBG_RELAX_OWNER_HEURISTICS") == "0"
    assert int(ov.get("SHOOTER_VOD_MAX_PANN_PROBE", "0")) >= 24
