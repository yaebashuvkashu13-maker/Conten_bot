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


def test_used_peak_times_montage_parts_int_does_not_crash() -> None:
    """Broken writers stored montage_parts as int count — must not TypeError."""
    peaks = used_peak_times_shooter(
        "LVe3yun9Mk8",
        {"LVe3yun9Mk8_100"},
        [
            {
                "segment_id": "LVe3yun9Mk8_100",
                "peak_start": 106.5,
                "montage_parts": 3,  # bug shape that crashed VPS
                "montage_peaks": [106.5, 180.5, 266.5],
            }
        ],
    )
    assert 106.5 in peaks
    assert 180.5 in peaks
    assert 266.5 in peaks


def test_used_peak_times_montage_parts_list() -> None:
    peaks = used_peak_times_shooter(
        "abc12345678",
        {"abc12345678_10"},
        [
            {
                "segment_id": "abc12345678_10",
                "peak_start": 15.0,
                "montage_parts": ["abc12345678_10", "abc12345678_80"],
            }
        ],
    )
    assert 15.0 in peaks
    assert 80.0 in peaks


def test_used_peak_times_montage_parts_not_multiplied() -> None:
    """Each part sid must not re-append the full montage_parts list (used=21 bug)."""
    parts = ["5XMoDWiXksA_33", "5XMoDWiXksA_71", "5XMoDWiXksA_105"]
    index = [
        {
            "segment_id": sid,
            "peak_start": peak,
            "montage_id": "5XMoDWiXksA_mtg_1",
            "montage_parts": parts,
        }
        for sid, peak in zip(parts, [40.8, 78.8, 112.8])
    ]
    peaks = used_peak_times_shooter("5XMoDWiXksA", set(parts), index)
    assert peaks == [33.0, 40.8, 71.0, 78.8, 105.0, 112.8]
    assert len(peaks) == 6


def test_segment_gap_softens_for_shooter_l2(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SHOOTER_VOD_SEGMENT_GAP_SEC", raising=False)
    monkeypatch.delenv("SHOOTER_VOD_SOFT_SEGMENT_GAP_SEC", raising=False)
    assert segment_gap_sec("pubg", soften_level=0) == 18.0
    assert segment_gap_sec("pubg", soften_level=2) == 7.0


def test_filter_blocked_peaks() -> None:
    avail, blocked = filter_blocked_peaks([114.0, 124.0], [116.0], gap_sec=7.0)
    assert 124.0 in avail
    assert 114.0 in blocked
