"""Tests for MLBB fight-boundary segmentation."""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))


class BannerLeadTest(unittest.TestCase):
    def tearDown(self) -> None:
        for k in (
            "MLBB_VOD_LEAD_SEC",
            "MLBB_KILL_BANNER_LEAD_SEC",
            "MLBB_BANNER_PRE_SEC",
            "MLBB_SAVAGE_BANNER_LEAD_SEC",
            "MLBB_MANIAC_BANNER_LEAD_SEC",
            "MLBB_TRIPLE_BANNER_LEAD_SEC",
        ):
            os.environ.pop(k, None)

    def test_savage_lead_covers_full_streak_not_just_triple(self) -> None:
        os.environ["MLBB_KILL_BANNER_LEAD_SEC"] = "16"
        from mlbb_fight_segment import banner_lead_sec

        # Default extra (~24) → ~40s before savage banner.
        self.assertGreaterEqual(banner_lead_sec(5), 40.0)
        self.assertGreaterEqual(banner_lead_sec(4), 28.0)
        # Explicit env may raise further, never shrink below base+extra floor via max(base, env).
        os.environ["MLBB_SAVAGE_BANNER_LEAD_SEC"] = "40"
        self.assertEqual(banner_lead_sec(5), 40.0)


class FightSegmentBoundsTest(unittest.TestCase):
    def tearDown(self) -> None:
        for k in (
            "MLBB_FIGHT_MAX_SEC",
            "MLBB_FIGHT_HARD_MAX_SEC",
            "MLBB_FIGHT_TRIM_LONG",
            "MLBB_KILL_BANNER_LEAD_SEC",
            "MLBB_BANNER_POST_SEC",
            "MLBB_FIGHT_MIN_SEC",
            "MLBB_BANNER_IDEAL_MIN",
            "MLBB_BANNER_HARD_POST_CUT",
        ):
            os.environ.pop(k, None)

    def test_hard_max_allows_longer_than_soft_max(self) -> None:
        os.environ["MLBB_FIGHT_MAX_SEC"] = "35"
        os.environ["MLBB_FIGHT_HARD_MAX_SEC"] = "65"
        os.environ["MLBB_FIGHT_TRIM_LONG"] = "0"
        fake_analysis = {
            "window_seconds": 2.0,
            "duration": 1200.0,
            "bins": 600,
            "center_motion": [0.9] * 600,
            "audio": [0.8] * 600,
            "scene": [0.5] * 600,
        }
        with patch("mlbb_fight_segment._analysis_for", return_value=fake_analysis):
            from mlbb_fight_segment import detect_fight_bounds

            start, end, dur = detect_fight_bounds(__import__("pathlib").Path("/tmp/fake.mp4"), 300.0)
        self.assertGreaterEqual(dur, 35.0)
        self.assertLessEqual(dur, 65.0)
        self.assertLess(start, end)

    def test_banner_bounds_single_not_extended_by_early_fight_start(self) -> None:
        """AJxzNqHrlyo_294: fight_start << banner-lead must not create 18s idle head."""
        os.environ["MLBB_KILL_BANNER_LEAD_SEC"] = "8"
        os.environ["MLBB_BANNER_POST_SEC"] = "3"
        os.environ["MLBB_FIGHT_MIN_SEC"] = "7"
        os.environ["MLBB_BANNER_IDEAL_MIN"] = "1"
        os.environ["MLBB_BANNER_HARD_POST_CUT"] = "1"
        from mlbb_kill_banner import bounds_from_banner

        start, end, dur = bounds_from_banner(
            312.0,
            429.0,
            fight_start=294.0,
            fight_end=320.0,
            banner_tier=1,
        )
        self.assertGreaterEqual(start, 303.9)
        self.assertLessEqual(end, 315.1)
        self.assertLessEqual(dur, 12.0)


if __name__ == "__main__":
    unittest.main()
