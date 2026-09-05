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
    monkeypatch.setenv("SHOOTER_VOD_MAX_SOFTEN_LEVEL", "4")
    assert soften_level(0) == 0
    assert soften_level(1) == 0
    assert soften_level(2) == 1
    assert soften_level(3) == 2
    assert soften_level(6) == 3
    assert soften_level(10) == 4


def test_adaptive_env_applies_visual_soften(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SHOOTER_VOD_ZERO_STREAK_SOFTEN", "2")
    monkeypatch.delenv("VISUAL_PUBG_MIN_FRAMES_PASS", raising=False)
    with adaptive_env(2) as level:
        assert level == 1
        assert os.environ["VISUAL_PUBG_MIN_FRAMES_PASS"] == "2"
    assert os.environ.get("VISUAL_PUBG_MIN_FRAMES_PASS") != "2"


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
    assert "SMART_PUBG_MIN_GUNFIRE_DENSITY" not in ov
    assert "VISUAL_MENU_OVERLAY_MAX" not in ov
    assert ov.get("PUBG_METRO_VOD_MIN_PROBES") == "1"
    assert ov.get("PUBG_METRO_SEGMENT_RELAX") == "1"
    assert float(ov["SMART_PUBG_MAX_CENTER_TEXT"]) > float(
        overrides_for_level(1)["SMART_PUBG_MAX_CENTER_TEXT"]
    )


def test_adaptive_env_reapplies_game_floors(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("SHOOTER_VOD_ZERO_STREAK_SOFTEN", "2")
    monkeypatch.setenv("VOD_ADAPTIVE_THRESH_DIR", str(tmp_path / "adaptive"))
    monkeypatch.setenv("VOD_SEGMENT_GAME", "pubg")
    monkeypatch.setenv("VOD_FORCE_SOFTEN", "1")
    monkeypatch.setenv("VOD_FORCE_GUN_DENSITY", "0.020")
    with adaptive_env(2) as level:
        assert level == 1
        assert float(os.environ["PUBG_SINGLE_MIN_GUN_DENSITY"]) == pytest.approx(0.020)
        assert "SMART_PUBG_MIN_GUNFIRE_DENSITY" not in overrides_for_level(level)


def test_l3_trusts_metro_vod_on_presend() -> None:
    ov = overrides_for_level(3)
    assert ov.get("PUBG_METRO_SEGMENT_TRUST_VOD") == "1"
    assert ov.get("PUBG_REJECT_BOT_FARM") == "0"


def test_l4_trusts_panns_and_more_probes() -> None:
    ov = overrides_for_level(4)
    # Delivery safety: relax heuristics stay off even at L4.
    assert ov.get("PUBG_RELAX_OWNER_HEURISTICS") == "0"
    assert int(ov.get("SHOOTER_VOD_MAX_PANN_PROBE", "0")) >= 24
    assert float(ov.get("PUBG_PANNS_TRUST_MIN", "0")) <= 0.30
