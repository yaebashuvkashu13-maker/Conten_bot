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


def test_soften_capped_quality_first(monkeypatch):
    monkeypatch.setenv("MLBB_VOD_ZERO_STREAK_SOFTEN", "3")
    monkeypatch.delenv("MLBB_VOD_MAX_SOFTEN_LEVEL", raising=False)
    monkeypatch.delenv("MLBB_VOD_DISABLE_SOFTEN", raising=False)
    assert soften_level(0) == 0
    assert soften_level(2) == 0
    assert soften_level(3) == 1
    assert soften_level(6) == 1  # capped — no L2 trash path


def test_soften_can_raise_ceiling(monkeypatch):
    monkeypatch.setenv("MLBB_VOD_ZERO_STREAK_SOFTEN", "3")
    monkeypatch.setenv("MLBB_VOD_MAX_SOFTEN_LEVEL", "2")
    assert soften_level(6) == 2


def test_quality_first_keeps_banner(monkeypatch):
    monkeypatch.setenv("MLBB_VOD_QUALITY_FIRST", "1")
    ov = overrides_for_level(1)
    assert ov["MLBB_KILL_BANNER_REQUIRED"] == "1"
    assert ov["MLBB_VOD_BANNER_PRESEND"] == "1"
    assert ov["MLBB_VOD_MOTION_ANCHOR_OK"] == "0"


def test_legacy_soften_without_quality_first(monkeypatch):
    monkeypatch.setenv("MLBB_VOD_QUALITY_FIRST", "0")
    ov = overrides_for_level(1)
    assert ov["MLBB_VOD_BANNER_PREFILTER"] == "0"
    assert ov["MLBB_KILL_BANNER_REQUIRED"] == "0"
    assert ov["MLBB_VOD_BANNER_PRESEND"] == "0"
    assert ov["MLBB_VOD_MOTION_ANCHOR_OK"] == "1"


def test_l2_lenient_uniform_for_presend_tail(monkeypatch):
    monkeypatch.setenv("MLBB_VOD_QUALITY_FIRST", "0")
    ov = overrides_for_level(2)
    assert ov["MLBB_VOD_LENIENT_UNIFORM"] == "1"
    assert float(ov["MLBB_VOD_TAIL_MIN_HUD_RATE"]) <= 0.40


def test_peak_near_skipped_import():
    from mlbb_vod_adaptive_gate import peak_near_skipped

    assert peak_near_skipped(100.0, {102.0}) is True
    assert peak_near_skipped(100.0, {200.0}) is False


def test_should_notify_only_on_level_up(monkeypatch):
    monkeypatch.setenv("MLBB_VOD_ZERO_STREAK_SOFTEN", "3")
    monkeypatch.setenv("MLBB_VOD_MAX_SOFTEN_LEVEL", "2")
    assert should_notify_soften(5, 1, prev_level=0) is True
    assert should_notify_soften(5, 1, prev_level=1) is False
    assert should_notify_soften(6, 2, prev_level=1) is True
    assert should_notify_soften(15, 2, prev_level=2) is False


def test_adaptive_env_restores(monkeypatch):
    monkeypatch.setenv("MLBB_KILL_BANNER_MIN_TIER", "double")
    monkeypatch.setenv("MLBB_VOD_ZERO_STREAK_SOFTEN", "3")
    monkeypatch.setenv("MLBB_VOD_QUALITY_FIRST", "0")
    with adaptive_env(3) as level:
        assert level == 1
        assert os.environ["MLBB_KILL_BANNER_MIN_TIER"] == "single"
    assert os.environ["MLBB_KILL_BANNER_MIN_TIER"] == "double"


def test_record_and_streak_helpers():
    state: dict = {"vod_outcomes": []}
    record_vod_outcome(state, vod_id="a", sent=0)
    record_vod_outcome(state, vod_id="b", sent=0)
    assert streak_from_state(state) == 2
    record_vod_outcome(state, vod_id="c", sent=1)
    assert streak_from_state(state) == 0
