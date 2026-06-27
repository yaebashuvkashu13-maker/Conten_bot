"""Tests for adaptive VOD gate after zero-cut streaks."""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from mlbb_vod_adaptive_gate import (  # noqa: E402
    adaptive_env,
    overrides_for_level,
    record_vod_outcome,
    should_notify_soften,
    soften_level,
    streak_from_state,
    trailing_zero_streak,
)


def test_trailing_zero_streak():
    hist = [{"sent": 1}, {"sent": 0}, {"sent": 0}, {"sent": 0}]
    assert trailing_zero_streak(hist) == 3
    assert trailing_zero_streak([{"sent": 0}, {"sent": 2}]) == 0


def test_soften_after_three_zeros():
    os.environ["MLBB_VOD_ZERO_STREAK_SOFTEN"] = "3"
    assert soften_level(0) == 0
    assert soften_level(2) == 0
    assert soften_level(3) == 1
    assert soften_level(5) == 1
    assert soften_level(6) == 2


def test_soft_overrides_keep_motion_not_banner_tier():
    ov = overrides_for_level(1)
    assert ov["MLBB_VOD_BANNER_PREFILTER"] == "0"
    assert "MLBB_KILL_BANNER_MIN_TIER" not in ov
    assert "MLBB_VOD_BANNER_DISCOVER" not in ov
    assert "MLBB_KILL_BANNER_REQUIRED" not in ov


def test_l2_skips_presend_banner():
    ov = overrides_for_level(2)
    assert ov["MLBB_VOD_BANNER_PRESEND"] == "0"


def test_l2_lenient_uniform_for_presend_tail():
    ov = overrides_for_level(2)
    assert ov["MLBB_VOD_LENIENT_UNIFORM"] == "1"
    assert float(ov["MLBB_VOD_TAIL_MIN_HUD_RATE"]) <= 0.40


def test_peak_near_skipped_import():
    from mlbb_vod_adaptive_gate import peak_near_skipped

    assert peak_near_skipped(100.0, {102.0}) is True
    assert peak_near_skipped(100.0, {200.0}) is False


def test_should_notify_only_on_level_up():
    os.environ["MLBB_VOD_ZERO_STREAK_SOFTEN"] = "3"
    assert should_notify_soften(5, 1, prev_level=0) is True
    assert should_notify_soften(5, 1, prev_level=1) is False
    assert should_notify_soften(6, 2, prev_level=1) is True
    assert should_notify_soften(15, 2, prev_level=2) is False


def test_adaptive_env_restores():
    os.environ["MLBB_PRESEND_MIN_MOTION"] = "0.020"
    os.environ["MLBB_VOD_ZERO_STREAK_SOFTEN"] = "3"
    with adaptive_env(3) as level:
        assert level == 1
        assert os.environ["MLBB_PRESEND_MIN_MOTION"] == "0.014"
    assert os.environ["MLBB_PRESEND_MIN_MOTION"] == "0.020"


def test_record_resets_streak_on_send():
    state: dict = {"vod_outcomes": [{"id": "a", "sent": 0}, {"id": "b", "sent": 0}]}
    streak = record_vod_outcome(state, vod_id="c", sent=1)
    assert streak == 0
    assert streak_from_state(state) == 0


def test_record_increments_streak():
    state: dict = {}
    record_vod_outcome(state, vod_id="a", sent=0)
    record_vod_outcome(state, vod_id="b", sent=0)
    streak = record_vod_outcome(state, vod_id="c", sent=0)
    assert streak == 3
