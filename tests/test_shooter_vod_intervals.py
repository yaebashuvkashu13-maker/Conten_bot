"""Shooter VOD interval dedupe — no second clip from the same fight."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from vod_scan_state import shooter_interval_blocked, used_intervals_for_shooter_vod  # noqa: E402


def test_used_intervals_from_labeled_segment() -> None:
    index = [
        {
            "segment_id": "YErkHdD25u4_130",
            "vod_id": "YErkHdD25u4",
            "start": 87.0,
            "duration": 45.0,
        }
    ]
    blocked = {"YErkHdD25u4_130"}
    intervals = used_intervals_for_shooter_vod("YErkHdD25u4", blocked, index)
    assert intervals == [(87.0, 132.0)]


def test_overlap_blocks_second_fight_clip() -> None:
    index = [
        {
            "segment_id": "YErkHdD25u4_130",
            "vod_id": "YErkHdD25u4",
            "start": 87.0,
            "duration": 45.0,
        }
    ]
    blocked = {"YErkHdD25u4_130"}
    reserved = used_intervals_for_shooter_vod("YErkHdD25u4", blocked, index)
    # Peak 118 clip: start 71, ~45s fight window overlaps sent 87-132.
    assert shooter_interval_blocked(71.0, 116.0, reserved) is True
    assert shooter_interval_blocked(140.0, 170.0, reserved) is False
