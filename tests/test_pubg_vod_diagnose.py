"""Tests for PUBG VOD diagnose + adaptive streak skip."""

from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

from mlbb_vod_adaptive_gate import record_vod_outcome, trailing_zero_streak  # noqa: E402


def test_trailing_zero_streak_skips_fast_reject() -> None:
    hist = [
        {"id": "a", "sent": 0, "streak_skip": True},
        {"id": "b", "sent": 0},
    ]
    assert trailing_zero_streak(hist) == 1


def test_record_vod_outcome_streak_skip() -> None:
    state: dict = {"vod_outcomes": [{"id": "x", "sent": 0}]}
    record_vod_outcome(state, vod_id="fast", sent=0, streak_skip=True)
    assert trailing_zero_streak(state["vod_outcomes"]) == 1
