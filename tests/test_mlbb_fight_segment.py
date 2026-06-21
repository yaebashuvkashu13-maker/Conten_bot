"""Tests for MLBB fight-boundary segmentation."""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))


class FightSegmentBoundsTest(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
