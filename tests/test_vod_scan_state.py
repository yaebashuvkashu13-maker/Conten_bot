"""Tests for vod_scan_state helpers."""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

from vod_scan_state import (  # noqa: E402
    max_peak_tries,
    pool_peaks_fully_blocked,
    record_vod_scan,
    should_mark_vod_exhausted,
    should_skip_vod_rescan,
    strict_peak_tries,
)


def test_should_mark_vod_exhausted() -> None:
    assert should_mark_vod_exhausted({"last_scan_blocked": True}) is True
    assert should_mark_vod_exhausted({"last_pool_peaks": []}) is True
    assert should_mark_vod_exhausted({"last_pool_peaks": [124.0], "last_scan_blocked": False}) is False
    assert should_mark_vod_exhausted({"last_scan_sent": 0}) is False


def test_strict_peak_tries_defaults() -> None:
    assert strict_peak_tries("mlbb") >= 2
    assert strict_peak_tries("pubg") >= 2


def test_max_peak_tries() -> None:
    assert max_peak_tries(0, game="pubg", soft_max_fn=lambda: 8) == strict_peak_tries("pubg")
    assert max_peak_tries(2, game="pubg", soft_max_fn=lambda: 8) == 8


def test_pool_fully_blocked_when_sent() -> None:
    blocked = pool_peaks_fully_blocked(
        [124.0],
        used_peaks=[116.0, 124.0],
        gap_sec=7.0,
        blocked_sids={"ICE7afoNgUA_120"},
        vod_id="ICE7afoNgUA",
    )
    assert blocked is True


def test_pool_not_blocked_when_new_peak() -> None:
    blocked = pool_peaks_fully_blocked(
        [200.0],
        used_peaks=[116.0, 124.0],
        gap_sec=7.0,
        blocked_sids={"ICE7afoNgUA_120", "ICE7afoNgUA_112"},
        vod_id="ICE7afoNgUA",
    )
    assert blocked is False


def test_scan_cooldown(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SHOOTER_VOD_SCAN_COOLDOWN_SEC", "3600")
    entry = {"last_scan_at": time.time(), "last_scan_sent": 0, "last_scan_blocked": True}
    assert should_skip_vod_rescan(entry) is True
    entry2 = {"last_scan_at": time.time() - 7200, "last_scan_sent": 0, "last_scan_blocked": True}
    assert should_skip_vod_rescan(entry2) is False


def test_record_vod_scan() -> None:
    entry: dict = {}
    record_vod_scan(entry, sent=0, pool_peaks=[124.0], blocked=True)
    assert entry["last_scan_blocked"] is True
    assert entry["last_pool_peaks"] == [124.0]
