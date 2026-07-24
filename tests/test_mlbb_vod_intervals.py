"""Interval overlap tests for MLBB VOD highlight dedupe."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from mlbb_vod_intervals import (
    conflicts_any_interval,
    fight_anchor_sec,
    interval_gap_sec,
    intervals_overlap,
    same_fight_anchor,
    segment_duration,
    segment_interval,
)


class VodIntervalOverlapTest(unittest.TestCase):
    def test_overlap_when_second_starts_before_first_ends(self) -> None:
        # 6:45 end vs 6:42 start — must overlap
        self.assertTrue(intervals_overlap(400.0, 405.0, 402.0, 430.0, gap=0.0))

    def test_no_overlap_when_second_starts_after_first_ends(self) -> None:
        self.assertFalse(intervals_overlap(400.0, 405.0, 406.0, 430.0, gap=0.0))

    def test_gap_required_between_highlights(self) -> None:
        self.assertTrue(intervals_overlap(400.0, 405.0, 407.0, 420.0, gap=3.0))
        self.assertFalse(intervals_overlap(400.0, 405.0, 408.0, 420.0, gap=3.0))

    def test_segment_interval_uses_fight_duration(self) -> None:
        row = {"start": 100.0, "fight_dur": 42.0, "clip": {"input_duration": 42.0}}
        start, end = segment_interval(row)
        self.assertEqual(start, 100.0)
        self.assertEqual(end, 142.0)

    def test_same_fight_anchor_by_banner(self) -> None:
        a = {"start": 282.0, "banner_sec": 334.0, "peak_start": 300.0, "kill_banner": "triple"}
        b = {"start": 318.0, "banner_sec": 334.0, "peak_start": 334.0, "kill_banner": "triple"}
        self.assertTrue(same_fight_anchor(a, b))
        self.assertEqual(fight_anchor_sec(a), 334.0)

    def test_near_adjacent_windows_conflict_with_default_gap(self) -> None:
        # Dhwn-style: 282→314 and 318→349 with default interval gap 12 → conflict
        self.assertTrue(intervals_overlap(282.0, 314.0, 318.0, 349.0, gap=interval_gap_sec()))


if __name__ == "__main__":
    unittest.main()
