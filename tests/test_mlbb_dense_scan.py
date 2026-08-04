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
        with patch("mlbb_vod_title.vod_title_blob", return_value="savage"):
            assert kb._discover_scan_start(vod, 600.0) == 3.0


def test_effective_discover_min_tier_title_override() -> None:
    old_title = os.environ.get("MLBB_VOD_TITLE_MIN_TIER")
    old_force = os.environ.get("MLBB_VOD_TITLE_FORCE_DISCOVER_TIER")
    old_cap = os.environ.get("MLBB_KILL_BANNER_DISCOVER_TITLE_CAP")
    old_merge = os.environ.get("MLBB_KILL_BANNER_DISCOVER_MERGE_TIER")
    os.environ["MLBB_VOD_TITLE_MIN_TIER"] = "5"
    os.environ["MLBB_VOD_TITLE_FORCE_DISCOVER_TIER"] = "0"
    os.environ["MLBB_KILL_BANNER_DISCOVER_MERGE_TIER"] = "1"
    os.environ.pop("MLBB_KILL_BANNER_DISCOVER_TITLE_CAP", None)
    try:
        # Title promises savage but discover merge stays capped at merge tier.
        assert kb._effective_discover_min_tier(None) == 1
        assert kb._effective_discover_min_tier(2) == 1
        os.environ["MLBB_VOD_TITLE_FORCE_DISCOVER_TIER"] = "1"
        assert kb._effective_discover_min_tier(None) == 5
    finally:
        for key, old in (
            ("MLBB_VOD_TITLE_MIN_TIER", old_title),
            ("MLBB_VOD_TITLE_FORCE_DISCOVER_TIER", old_force),
            ("MLBB_KILL_BANNER_DISCOVER_TITLE_CAP", old_cap),
            ("MLBB_KILL_BANNER_DISCOVER_MERGE_TIER", old_merge),
        ):
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old


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
