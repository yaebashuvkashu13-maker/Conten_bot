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
    minimal_pool_from_entry,
    pool_cache_valid,
    pool_peaks_fully_blocked,
    record_vod_scan,
    scan_zero_detail,
    should_mark_vod_exhausted,
    should_skip_vod_rescan,
    strict_peak_tries,
)


def test_should_mark_vod_exhausted() -> None:
    assert should_mark_vod_exhausted({"last_scan_blocked": True}) is True
    assert should_mark_vod_exhausted({"last_pool_peaks": []}) is True
    assert should_mark_vod_exhausted({"last_pool_peaks": [124.0], "last_scan_blocked": False}) is False
    assert should_mark_vod_exhausted({"last_scan_sent": 0}) is False
    assert should_mark_vod_exhausted({"last_pool_peaks": [124.0], "zero_send_attempts": 5}) is True


def test_scan_zero_detail() -> None:
    assert "пики" in scan_zero_detail({"last_scan_blocked": True})
    assert "pool=0" in scan_zero_detail({"last_pool_peaks": []})
    assert scan_zero_detail({"last_pool_peaks": [120.0], "last_scan_blocked": False}) == (
        "presend отклонил пики (pool=1)"
    )


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
    assert entry["last_pool_peaks"][0]["peak_sec"] == 124.0


def test_pool_cache_valid_and_minimal_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VOD_POOL_TTL_SEC", "3600")
    entry = {
        "last_pool_at": time.time(),
        "last_pool_peaks": [
            {"peak_sec": 120.0, "score": 0.8, "blocked_reason": ""},
            {"peak_sec": 200.0, "score": 0.4, "blocked_reason": "presend"},
        ],
    }
    assert pool_cache_valid(entry) is True
    pool = minimal_pool_from_entry(entry)
    assert len(pool) == 1
    assert pool[0]["start"] == 120.0


def test_pool_cache_expired(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VOD_POOL_TTL_SEC", "60")
    entry = {"last_pool_at": time.time() - 120, "last_pool_peaks": [{"peak_sec": 1.0, "score": 0, "blocked_reason": ""}]}
    assert pool_cache_valid(entry) is False
