#!/usr/bin/env python3
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import mlbb_kill_banner as kb  # noqa: E402


def test_discover_scan_start_uses_title_early() -> None:
    vod = Path("/tmp/yt_savage_test.mp4")
    with patch("mlbb_vod_title.title_scan_start_sec", return_value=3.0):
        assert kb._discover_scan_start(vod, 600.0) == 3.0


def test_effective_discover_min_tier_title_override() -> None:
    old = os.environ.get("MLBB_VOD_TITLE_MIN_TIER")
    os.environ["MLBB_VOD_TITLE_MIN_TIER"] = "5"
    try:
        assert kb._effective_discover_min_tier(None) == 5
        assert kb._effective_discover_min_tier(2) == 5
    finally:
        if old is None:
            os.environ.pop("MLBB_VOD_TITLE_MIN_TIER", None)
        else:
            os.environ["MLBB_VOD_TITLE_MIN_TIER"] = old


def test_dense_scan_enabled() -> None:
    old = os.environ.get("MLBB_VOD_BANNER_DENSE_SEC")
    os.environ["MLBB_VOD_BANNER_DENSE_SEC"] = "1"
    try:
        assert kb._dense_scan_enabled() is True
    finally:
        if old is None:
            os.environ.pop("MLBB_VOD_BANNER_DENSE_SEC", None)
        else:
            os.environ["MLBB_VOD_BANNER_DENSE_SEC"] = old


def test_dense_scan_end_caps_savage_title() -> None:
    vod = Path("/tmp/yt_savage_cap.mp4")
    with patch("mlbb_vod_title.title_min_banner_tier", return_value=5):
        end = kb._dense_scan_end(vod, 780.0, 3.0)
        assert end <= 3.0 + 360.0 + 1


def test_scan_policy_resets_dense_for_generic_vod(monkeypatch) -> None:
    from mlbb_vod_segment_feed import _configure_banner_scan_policy

    monkeypatch.setenv("MLBB_VOD_BANNER_DENSE_SEC", "1")
    monkeypatch.setenv("MLBB_KILL_BANNER_SPARSE_MAX_SEC", "120")
    assert _configure_banner_scan_policy(0) is False
    assert os.environ["MLBB_VOD_BANNER_DENSE_SEC"] == "0"
    assert os.environ["MLBB_KILL_BANNER_DISCOVER_MAX_SEC"] == "120"


def test_scan_policy_enables_dense_for_maniac_title(monkeypatch) -> None:
    from mlbb_vod_segment_feed import _configure_banner_scan_policy

    monkeypatch.setenv("MLBB_KILL_BANNER_DENSE_MAX_SEC", "360")
    assert _configure_banner_scan_policy(4) is True
    assert os.environ["MLBB_VOD_BANNER_DENSE_SEC"] == "1"
    assert os.environ["MLBB_KILL_BANNER_DISCOVER_STEP"] == "1"
    assert os.environ["MLBB_KILL_BANNER_DISCOVER_MAX_SEC"] == "360"


def test_pick_available_vod_returns_row_not_duration() -> None:
    from mlbb_vod_segment_feed import _pick_available_vod

    registry = [
        {
            "id": "abc123",
            "path": "/nonexistent/yt_abc123.mp4",
            "last_scan_at": 0,
            "title_rescan_priority": True,
        }
    ]
    with patch("mlbb_vod_segment_feed._ffprobe_duration", return_value=780.0):
        with patch("mlbb_vod_segment_feed._vod_length_ok", return_value=True):
            with patch("mlbb_vod_segment_feed.should_skip_vod_rescan", return_value=False):
                with patch.object(Path, "exists", return_value=True):
                    pick = _pick_available_vod(registry)
    assert pick is not None
    assert pick.get("id") == "abc123"

