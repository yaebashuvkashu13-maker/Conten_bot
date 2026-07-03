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
    assert intervals == [(83.0, 132.0)]


def test_overlap_blocks_second_fight_clip() -> None:
    index = [
        {
            "segment_id": "YErkHdD25u4_130",
            "vod_id": "YErkHdD25u4",
            "start": 87.0,
            "duration": 45.0,
            "peak_start": 134,
        }
    ]
    blocked = {"YErkHdD25u4_130"}
    reserved = used_intervals_for_shooter_vod("YErkHdD25u4", blocked, index)
    # Peak 118 clip: start 71, ~45s fight window overlaps sent 87-132.
    assert shooter_interval_blocked(71.0, 116.0, reserved) is True
    assert shooter_interval_blocked(150.0, 180.0, reserved) is False


def test_peak_fight_span_blocks_nearby_peak() -> None:
    from vod_scan_state import shooter_peak_fight_blocked

    used = [104.0]
    # Peak 96 is 8s from 104 — same fight, must block (span 45 * 0.85 > 8).
    assert shooter_peak_fight_blocked(96.0, used, game="pubg", soften_gap=7.0) is True
    assert shooter_peak_fight_blocked(160.0, used, game="pubg", soften_gap=7.0) is False


def test_6b07_overlap_case() -> None:
    index = [
        {
            "segment_id": "6b07LK4AZco_100",
            "vod_id": "6b07LK4AZco",
            "start": 57.0,
            "duration": 45.0,
            "peak_start": 104,
        }
    ]
    blocked = {"6b07LK4AZco_100"}
    reserved = used_intervals_for_shooter_vod("6b07LK4AZco", blocked, index)
    assert shooter_interval_blocked(49.0, 94.0, reserved) is True


def test_quvo_adjacent_fight_blocked() -> None:
    """_104 (103-148) then _56 (51-96) — only 7s gap, same engagement."""
    reserved = [(103.0, 148.0)]
    assert shooter_interval_blocked(51.0, 96.0, reserved) is True
    assert shooter_interval_blocked(160.0, 190.0, reserved) is False
