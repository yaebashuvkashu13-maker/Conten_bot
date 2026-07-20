"""Tests for stale pending_download recovery in MLBB feed."""

from __future__ import annotations

import sys
import time
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import mlbb_vod_segment_feed as feed  # noqa: E402


def test_pending_download_stale_without_ts() -> None:
    assert feed._pending_download_stale({"status": "downloading"}) is True
    assert feed._pending_download_stale({"status": "ready"}) is False


def test_pending_download_stale_by_ttl(monkeypatch) -> None:
    monkeypatch.setenv("MLBB_VOD_PENDING_DOWNLOAD_TTL_SEC", "60")
    fresh = {"status": "downloading", "started_ts": time.time()}
    old = {"status": "downloading", "started_ts": time.time() - 120}
    assert feed._pending_download_stale(fresh) is False
    assert feed._pending_download_stale(old) is True


def test_clear_stale_pending_download() -> None:
    state = {"pending_download": {"status": "downloading"}}
    out = feed._clear_stale_pending_download(state)
    assert out["pending_download"]["status"] == "stale_cleared"
