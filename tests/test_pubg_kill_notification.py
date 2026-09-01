from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import cv2
import numpy as np

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

from pubg_kill_notification import (  # noqa: E402
    locate_notification_regions,
    score_kill_notification_segment,
)


def _notification_frame(x: int, y: int) -> np.ndarray:
    frame = np.zeros((360, 640, 3), dtype=np.uint8)
    cv2.putText(
        frame,
        "Player_One  AKM  Enemy_2",
        (x, y),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.55,
        (255, 210, 50),
        2,
        cv2.LINE_AA,
    )
    return frame


def test_locator_finds_blue_notification_at_movable_positions() -> None:
    left = locate_notification_regions(_notification_frame(20, 80), ocr=False)
    right = locate_notification_regions(_notification_frame(300, 250), ocr=False)
    assert left and right
    assert left[0]["box"][0] < 0.2
    assert right[0]["box"][0] > 0.3
    assert left[0]["geometric_score"] > 0.25


def test_segment_score_prefers_transient_notification() -> None:
    frames = [np.zeros((360, 640, 3), dtype=np.uint8) for _ in range(10)]
    calls = 0

    def locate(_frame, **_kwargs):
        nonlocal calls
        calls += 1
        if calls in (3, 4, 5):
            return [{"score": 0.8, "text": "A AKM B", "box": [0.2, 0.1, 0.3, 0.05]}]
        return []

    with patch("pubg_kill_notification._decode_sample_frames", return_value=frames), patch(
        "pubg_kill_notification.locate_notification_regions",
        side_effect=locate,
    ), patch("gameplay_gate.detect_game_viewport_crop", return_value=None):
        score, report = score_kill_notification_segment(Path("/tmp/vod.mp4"), 10, 14)
    assert score > 0.6
    assert report["notification_hits"] == 3
    assert report["notification_text"] == "A AKM B"


def test_static_blue_hud_is_penalized() -> None:
    frames = [np.zeros((360, 640, 3), dtype=np.uint8) for _ in range(10)]
    region = [{"score": 0.8, "text": "STATIC", "box": [0.2, 0.1, 0.3, 0.05]}]
    with patch("pubg_kill_notification._decode_sample_frames", return_value=frames), patch(
        "pubg_kill_notification.locate_notification_regions",
        return_value=region,
    ), patch("gameplay_gate.detect_game_viewport_crop", return_value=None):
        score, report = score_kill_notification_segment(Path("/tmp/vod.mp4"), 10, 14)
    assert score < 0.45
    assert report["notification_hit_ratio"] == 1.0
