"""Tests for PUBG adaptive gate after zero-clip streaks."""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from pubg_adaptive_gate import (  # noqa: E402
    adaptive_env,
    overrides_for_level,
    record_vod_outcome,
    soften_level,
    streak_from_state,
    trailing_zero_streak,
)


def test_trailing_zero_streak():
    hist = [{"clips": 3}, {"clips": 0}, {"clips": 0}]
    assert trailing_zero_streak(hist) == 2


def test_soften_after_three_zeros():
    os.environ["PUBG_ZERO_STREAK_SOFTEN"] = "3"
    assert soften_level(2) == 0
    assert soften_level(3) == 1
    assert soften_level(6) == 2


def test_l1_lowers_gun_and_pann():
    ov = overrides_for_level(1)
    assert float(ov["SMART_PUBG_MIN_GUNFIRE_DENSITY"]) < 0.068
    assert float(ov["PUBG_COMBAT_PANN_MIN"]) < 0.24
    assert ov["PUBG_REJECT_BOT_FARM"] == "0"


def test_l2_disables_bot_farm():
    ov = overrides_for_level(2)
    assert ov["PUBG_REJECT_BOT_FARM"] == "0"
    assert float(ov["SMART_PUBG_MIN_GUNFIRE_DENSITY"]) < float(
        overrides_for_level(1)["SMART_PUBG_MIN_GUNFIRE_DENSITY"]
    )


def test_adaptive_env_restores():
    os.environ["PUBG_COMBAT_PANN_MIN"] = "0.24"
    os.environ["PUBG_ZERO_STREAK_SOFTEN"] = "3"
    with adaptive_env(3) as level:
        assert level == 1
        assert float(os.environ["PUBG_COMBAT_PANN_MIN"]) == 0.18
    assert os.environ["PUBG_COMBAT_PANN_MIN"] == "0.24"


def test_record_resets_on_clips():
    state: dict = {"vod_outcomes": [{"id": "a", "clips": 0}, {"id": "b", "clips": 0}]}
    streak = record_vod_outcome(state, vod_id="c", clips=3)
    assert streak == 0
    assert streak_from_state(state) == 0
