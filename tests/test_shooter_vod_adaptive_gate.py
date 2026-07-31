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
    streak_threshold,
)


def test_soften_after_two_zero_vods(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SHOOTER_VOD_ZERO_STREAK_SOFTEN", "2")
    assert soften_level(0) == 0
    assert soften_level(1) == 0
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
        assert float(os.environ["VIRAL_COMBAT_HOOK_MIN"]) <= 0.06
    assert "VISUAL_MENU_OVERLAY_MAX" not in os.environ or os.environ.get("VISUAL_MENU_OVERLAY_MAX") != "0.58"


def test_l1_relaxes_combat_hook() -> None:
    ov = overrides_for_level(1)
    assert float(ov["VIRAL_COMBAT_HOOK_MIN"]) <= 0.06
    assert float(ov["VIRAL_SEGMENT_HOOK_MIN"]) <= 0.10


def test_record_vod_outcome_streak() -> None:
    state: dict = {}
    record_vod_outcome(state, vod_id="a", sent=0)
    record_vod_outcome(state, vod_id="b", sent=0)
    assert streak_from_state(state) == 2
    record_vod_outcome(state, vod_id="c", sent=1)
    assert streak_from_state(state) == 0


def test_l2_keeps_pov_and_multi_frame() -> None:
    ov = overrides_for_level(2)
    assert ov.get("PUBG_POV_GATE") == "1"
    assert ov.get("VISUAL_PUBG_MIN_FRAMES_PASS") == "2"
    assert float(ov["VISUAL_MENU_OVERLAY_MAX"]) > float(overrides_for_level(1)["VISUAL_MENU_OVERLAY_MAX"])
    assert ov.get("PUBG_METRO_VOD_MIN_PROBES") == "1"
    assert ov.get("PUBG_METRO_SEGMENT_RELAX") == "1"


def test_l3_keeps_bot_farm_and_pov() -> None:
    ov = overrides_for_level(3)
    assert ov.get("PUBG_METRO_SEGMENT_TRUST_VOD") == "1"
    assert ov.get("PUBG_REJECT_BOT_FARM") == "1"
    assert ov.get("PUBG_POV_GATE") == "1"


def test_l4_trusts_panns_and_more_probes() -> None:
    ov = overrides_for_level(4)
    assert ov.get("PUBG_RELAX_OWNER_HEURISTICS") == "2"
    assert int(ov.get("SHOOTER_VOD_MAX_PANN_PROBE", "0")) >= 24
    assert float(ov.get("PUBG_PANNS_TRUST_MIN", "0")) <= 0.30
    assert ov.get("PUBG_REJECT_BOT_FARM") == "1"
    assert ov.get("PUBG_POV_GATE") == "1"
