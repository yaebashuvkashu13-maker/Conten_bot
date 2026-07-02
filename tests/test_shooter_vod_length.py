"""Shooter VOD length gate — reject streams outside 3–20 min."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

from shooter_vod_segment_feed import _reject_vod_length  # noqa: E402


def test_reject_vod_length_too_long(tmp_path: Path) -> None:
    vod = tmp_path / "yt_long.mp4"
    vod.write_bytes(b"")
    entry: dict = {}
    with patch("shooter_vod_segment_feed._ffprobe_duration", return_value=3200.0):
        reason = _reject_vod_length(vod, entry)
    assert reason == "vod_length=3200s"
    assert entry["exhausted"] is True


def test_reject_vod_length_ok(tmp_path: Path) -> None:
    vod = tmp_path / "yt_ok.mp4"
    vod.write_bytes(b"")
    entry: dict = {}
    with patch("shooter_vod_segment_feed._ffprobe_duration", return_value=600.0):
        reason = _reject_vod_length(vod, entry)
    assert reason is None
    assert entry == {}
