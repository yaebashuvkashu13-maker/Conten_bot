#!/usr/bin/env python3
"""Tests for shared VOD peak-gap helpers."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

from vod_peak_gap import (  # noqa: E402
    filter_blocked_peaks,
    peak_too_close,
    segment_gap_sec,
    used_peak_times_shooter,
)


def test_peak_too_close() -> None:
    assert peak_too_close(124.0, [116.0], 8.0) is True
    assert peak_too_close(124.0, [116.0], 7.0) is False


def test_used_peak_times_prefers_peak_start() -> None:
    peaks = used_peak_times_shooter(
        "ICE7afoNgUA",
        {"ICE7afoNgUA_112"},
        [{"segment_id": "ICE7afoNgUA_112", "peak_start": 116, "start": 112}],
    )
    assert peaks == [116.0]


def test_segment_gap_softens_for_shooter_l2(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SHOOTER_VOD_SEGMENT_GAP_SEC", raising=False)
    monkeypatch.delenv("SHOOTER_VOD_SOFT_SEGMENT_GAP_SEC", raising=False)
    assert segment_gap_sec("pubg", soften_level=0) == 18.0
    assert segment_gap_sec("pubg", soften_level=2) == 7.0


def test_filter_blocked_peaks() -> None:
    avail, blocked = filter_blocked_peaks([114.0, 124.0], [116.0], gap_sec=7.0)
    assert 124.0 in avail
    assert 114.0 in blocked
