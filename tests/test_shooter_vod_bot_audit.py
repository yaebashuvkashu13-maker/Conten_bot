"""Tests for shooter_vod_bot_audit."""

from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

from shooter_vod_bot_audit import _wrong_fast_exhaust  # noqa: E402


def test_wrong_fast_exhaust_strong_top() -> None:
    entry = {
        "exhausted": True,
        "reject_reason": "fast_panns_1/1 top=0.473 min_hits=2",
    }
    assert _wrong_fast_exhaust(entry) is True


def test_wrong_fast_exhaust_dead_vod() -> None:
    entry = {
        "exhausted": True,
        "reject_reason": "fast_panns_0/8 top=0.006 min=0.14",
    }
    assert _wrong_fast_exhaust(entry) is False
