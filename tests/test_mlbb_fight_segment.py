"""Tests for MLBB fight-boundary segmentation."""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))


class IdealClipSpecTest(unittest.TestCase):
    def test_ideal_clip_min_includes_lead_fight_post(self) -> None:
        os.environ["MLBB_VOD_LEAD_SEC"] = "4"
        os.environ["MLBB_FIGHT_MIN_SEC"] = "8"
        os.environ["MLBB_FIGHT_POST_SEC"] = "4"
        from mlbb_fight_segment import ideal_clip_min_sec

        self.assertEqual(ideal_clip_min_sec(), 16.0)

    def test_k8u1a_style_8s_clip_below_ideal(self) -> None:
        """Regression: k8u1a-xri2g_900 was 8s — must fail ideal min (4+8+4=16)."""
        os.environ["MLBB_VOD_LEAD_SEC"] = "4"
        os.environ["MLBB_FIGHT_MIN_SEC"] = "8"
        os.environ["MLBB_FIGHT_POST_SEC"] = "4"
        from mlbb_fight_segment import ideal_clip_min_sec

        sent_dur = 8.0
        self.assertLess(sent_dur, ideal_clip_min_sec())


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


class ClipActionSustainTest(unittest.TestCase):
    def test_short_clip_passes(self) -> None:
        from mlbb_fight_segment import clip_action_sustain_ok

        ok, reason = clip_action_sustain_ok(Path("/tmp/x.mp4"), 0.0, 4.0)
        self.assertTrue(ok)
        self.assertEqual(reason, "short_clip_ok")

    def test_idle_tail_rejected(self) -> None:
        from mlbb_fight_segment import clip_action_sustain_ok

        with patch("gameplay_gate.score_segment_combat", return_value=(0.001, 0.001, 0.0, "")):
            ok, reason = clip_action_sustain_ok(Path("/tmp/x.mp4"), 100.0, 20.0)
        self.assertFalse(ok)
        self.assertIn("idle_death_tail", reason)


if __name__ == "__main__":
    unittest.main()
