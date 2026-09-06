#!/usr/bin/env python3
"""Media integration tests — require short real mp4 fixtures (skipped if missing)."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "pubg"
FIXTURE_NAMES = {
    "kill_with_notification.mp4": "kill",
    "shooting_no_kill.mp4": "no_kill",
    "loot_walk.mp4": "loot",
    "author_death.mp4": "death",
    "movable_blue_notification.mp4": "kill",
    "blue_hud_false_positive.mp4": "hud_fp",
    "nonstandard_viewport.mp4": "kill",
}


def _fixture(name: str) -> Path | None:
    path = FIXTURES / name
    return path if path.is_file() and path.stat().st_size > 1024 else None


@pytest.mark.media
@pytest.mark.parametrize("filename,expected", list(FIXTURE_NAMES.items()))
def test_pubg_quality_gate(filename: str, expected: str) -> None:
    path = _fixture(filename)
    if path is None:
        pytest.skip(f"fixture missing: {FIXTURES / filename}")
    from pubg_quality_score import score_pubg_window

    ok, reason, report = score_pubg_window(path, 30.0, 14.0)
    if expected == "kill":
        assert report.get("payoff_score", 0) >= 0.30 or ok, reason
    elif expected == "no_kill":
        assert not ok or report.get("payoff_score", 1) < 0.50
    elif expected == "loot":
        assert not ok or report.get("fight_score", 1) < 0.55
    elif expected == "death":
        assert not ok
    elif expected == "hud_fp":
        assert report.get("kill_notification_hit") is not True


@pytest.mark.media
def test_kill_notification_detector_on_fixture() -> None:
    path = _fixture("movable_blue_notification.mp4") or _fixture("kill_with_notification.mp4")
    if path is None:
        pytest.skip("no kill notification fixture")
    from pubg_kill_notification import score_kill_notification_segment

    score, meta = score_kill_notification_segment(path, 20.0, 12.0)
    assert score >= 0.20 or meta.get("notification_hits", 0) > 0
