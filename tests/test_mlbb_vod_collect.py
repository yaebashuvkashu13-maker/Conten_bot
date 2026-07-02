"""Tests for VOD scan segment collection and presend retry logic."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

# mlbb_vod_segment_feed pulls gameplay_gate → cv2
sys.modules.setdefault("cv2", MagicMock())
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from mlbb_vod_adaptive_gate import peak_near_skipped  # noqa: E402


def test_peak_near_skipped_tolerance():
    skip = {384.0, 582.0}
    assert peak_near_skipped(384.0, skip) is True
    assert peak_near_skipped(386.0, skip) is True
    assert peak_near_skipped(390.0, skip) is False


def test_collect_scan_skips_rejected_peaks(tmp_path: Path):
    from mlbb_vod_segment_feed import _collect_scan_segments

    vod = tmp_path / "yt_test.mp4"
    vod.write_bytes(b"x")

    pool = [
        {
            "start": 384.0,
            "score": 0.5,
            "highlight_metrics": {"rule_pass": True, "pass_reason": "mlbb_fight_ok", "clip_score": 0.2},
        },
        {
            "start": 582.0,
            "score": 0.6,
            "highlight_metrics": {"rule_pass": True, "pass_reason": "mlbb_fight_ok", "clip_score": 0.25},
        },
    ]

    fake_clip = {
        "start": 10.0,
        "peak_start": 582.0,
        "input_duration": 12.0,
        "output_duration": 12.0,
        "source_path": str(vod),
        "source_index": 0,
        "speed": 1.0,
        "anchor": "motion",
    }

    os.environ["MLBB_VOD_MIN_PEAK_SEC"] = "0"
    os.environ["MLBB_VOD_SEND_ONE"] = "1"
    os.environ["MLBB_KILL_BANNER_REQUIRED"] = "0"

    with (
        patch("mlbb_vod_segment_feed.discover_strict_candidates", return_value=pool),
        patch("mlbb_vod_segment_feed._normalize_clip", return_value=fake_clip),
        patch("mlbb_vod_segment_feed.labeled_ids", return_value={}),
        patch("mlbb_vod_segment_feed.load_feed_sent", return_value=set()),
        patch("mlbb_vod_segment_feed._used_intervals_for_vod", return_value=[]),
    ):
        first, cached = _collect_scan_segments(vod, "sig", {}, set(), 12)
        assert len(first) == 1
        assert first[0]["peak_start"] == 582.0

        second, _ = _collect_scan_segments(
            vod, "sig", {}, set(), 12, pool=cached, skip_peaks={384.0}
        )
        assert len(second) == 1
        assert second[0]["peak_start"] == 582.0

        third, _ = _collect_scan_segments(
            vod, "sig", {}, set(), 12, pool=cached, skip_peaks={384.0, 582.0}
        )
        assert third == []
