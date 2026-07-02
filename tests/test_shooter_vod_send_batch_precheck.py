"""Tests for shooter VOD pre-render presend check."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))


def test_send_batch_skips_render_when_precheck_rejects(tmp_path: Path, monkeypatch) -> None:
    import shooter_vod_segment_feed as feed

    monkeypatch.setenv("DAILY_GAME_CYCLE_ENABLED", "0")
    vod = tmp_path / "yt_test.mp4"
    vod.write_bytes(b"x")
    row = {
        "segment_id": "X_120",
        "start": 120.0,
        "peak_start": 124.0,
        "duration": 15.0,
        "clip": {"start": 120.0, "duration": 15.0},
    }
    with patch.object(feed, "_validate_shooter_candidate_pre_render", return_value=(False, "no_shots", {})):
        with patch.object(feed, "render_single_segment") as render:
            n = feed._send_batch("pubg", "t", "c", vod, [row], sig="s")
    assert n == 0
    assert render.call_count == 0

