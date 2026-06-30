"""Tests for vod_scan_state helpers."""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

from vod_scan_state import (  # noqa: E402
    classic_outdoor_vod_reject,
    pool_peaks_fully_blocked,
    record_vod_scan,
    should_skip_vod_rescan,
)


def test_classic_outdoor_reject() -> None:
    reason = "metro_vod_reject=0/3 (90s:classic_outdoor_sky=3/3;165s:classic_outdoor_sky=3/3)"
    assert classic_outdoor_vod_reject(reason) is True
    assert classic_outdoor_vod_reject("metro_vod_ok=1/3 (90s:metro_underground)") is False


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
