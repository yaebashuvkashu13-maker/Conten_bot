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
    assert soften_level(12) == 2
    assert soften_level(13) == 3
    assert soften_level(40) == 3


def test_soft_overrides_disable_banner_prefilter():
    ov = overrides_for_level(1)
    assert ov["MLBB_VOD_BANNER_PREFILTER"] == "0"
    assert ov["MLBB_KILL_BANNER_MIN_TIER"] == "single"
    assert ov["MLBB_KILL_BANNER_REQUIRED"] == "0"


def test_l1_skips_presend_banner_and_motion_anchor():
    ov = overrides_for_level(1)
    assert ov["MLBB_VOD_BANNER_PRESEND"] == "0"
    assert ov["MLBB_VOD_MOTION_ANCHOR_OK"] == "1"


def test_l2_skips_presend_banner():
    ov = overrides_for_level(2)
    assert ov["MLBB_VOD_BANNER_PRESEND"] == "0"


def test_l2_lenient_uniform_for_presend_tail():
    ov = overrides_for_level(2)
    assert ov["MLBB_VOD_LENIENT_UNIFORM"] == "1"
    assert float(ov["MLBB_VOD_TAIL_MIN_HUD_RATE"]) <= 0.40
    assert ov["MLBB_VOD_MIN_PEAK_SEC"] == "180"


def test_l3_allows_early_peaks_and_zero_clip_floor():
    ov = overrides_for_level(3)
    assert ov["MLBB_VOD_MIN_PEAK_SEC"] == "90"
    assert ov["MLBB_VOD_MIN_CLIP_SCORE"] == "0"
    assert ov["MLBB_VOD_SKIP_REVALIDATE"] == "0"
    assert ov["HIGHLIGHT_MAX_PANN_PROBE"] == "12"


def test_peak_near_skipped_import():
    from mlbb_vod_adaptive_gate import peak_near_skipped

    assert peak_near_skipped(100.0, {102.0}) is True
    assert peak_near_skipped(100.0, {200.0}) is False


def test_should_notify_only_on_level_up():
    os.environ["MLBB_VOD_ZERO_STREAK_SOFTEN"] = "3"
    assert should_notify_soften(5, 1, prev_level=0) is True
    assert should_notify_soften(5, 1, prev_level=1) is False
    assert should_notify_soften(6, 2, prev_level=1) is True
    assert should_notify_soften(13, 3, prev_level=2) is True
    assert should_notify_soften(15, 3, prev_level=3) is False


def test_adaptive_env_restores():
    os.environ["MLBB_KILL_BANNER_MIN_TIER"] = "double"
    os.environ["MLBB_VOD_ZERO_STREAK_SOFTEN"] = "3"
    with adaptive_env(3) as level:
        assert level == 1
        assert os.environ["MLBB_KILL_BANNER_MIN_TIER"] == "single"
    assert os.environ["MLBB_KILL_BANNER_MIN_TIER"] == "double"


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


def test_streak_from_state_uses_legacy_zero_cut_streak():
    state = {"zero_cut_streak": 12, "vod_outcomes": []}
    assert streak_from_state(state) == 12
    state2 = {"zero_cut_streak": 2, "vod_outcomes": [{"sent": 0}, {"sent": 0}, {"sent": 0}]}
    assert streak_from_state(state2) == 3
    state3 = {"zero_cut_streak": 40, "vod_outcomes": [{"sent": 0}] * 3}
    assert streak_from_state(state3) == 40
