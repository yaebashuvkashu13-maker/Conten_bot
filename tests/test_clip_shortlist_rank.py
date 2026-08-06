"""Unit tests for final CLIP shortlist ranking (no full-VOD hang path)."""

from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest import mock


class RankShortlistWithClipTests(unittest.TestCase):
    def test_disabled_returns_original(self) -> None:
        from highlight_scorer import rank_shortlist_with_clip

        rows = [{"segment_id": "a", "score": 1.0, "peak_start": 10.0}]
        out = rank_shortlist_with_clip(Path("/tmp/x.mp4"), rows, "standoff", enabled=False)
        self.assertIs(out, rows)

    def test_empty_rows(self) -> None:
        from highlight_scorer import rank_shortlist_with_clip

        out = rank_shortlist_with_clip(Path("/tmp/x.mp4"), [], "pubg", enabled=True)
        self.assertEqual(out, [])

    def test_orders_strong_clip_first(self) -> None:
        from highlight_scorer import rank_shortlist_with_clip

        rows = [
            {"segment_id": "weak", "score": 0.9, "peak_start": 10.0},
            {"segment_id": "strong", "score": 0.5, "peak_start": 80.0},
            {"segment_id": "mid", "score": 0.7, "peak_start": 160.0},
        ]

        def fake_score(_path, start, _dur, _profile, force=False):
            self.assertTrue(force)
            # strong peak around 80
            if 70 <= start <= 90:
                return 0.42, []
            if 150 <= start <= 170:
                return 0.15, []
            return 0.02, []

        with mock.patch("highlight_scorer._clip_bundle", return_value=(None, None, None, "cpu")):
            with mock.patch("highlight_scorer.score_clip_exemplar", side_effect=fake_score):
                with mock.patch.dict(
                    os.environ,
                    {
                        "SHOOTER_VOD_MONTAGE_CLIP_RANK": "1",
                        "SHOOTER_VOD_MONTAGE_CLIP_MIN": "0.10",
                        "SHOOTER_VOD_MONTAGE_CLIP_TIMEOUT_SEC": "30",
                    },
                    clear=False,
                ):
                    out = rank_shortlist_with_clip(Path("/tmp/x.mp4"), rows, "standoff", max_n=3)

        self.assertEqual(out[0]["segment_id"], "strong")
        self.assertGreaterEqual(float(out[0]["clip_score"]), 0.10)
        self.assertTrue(out[0]["highlight_metrics"]["clip_rank"])

    def test_timeout_keeps_panns_fallback(self) -> None:
        from highlight_scorer import rank_shortlist_with_clip

        rows = [{"segment_id": f"r{i}", "score": float(3 - i), "peak_start": float(i * 60)} for i in range(3)]

        def hang(*_a, **_k):
            import time

            time.sleep(2.0)
            return 0.2, []

        with mock.patch("highlight_scorer._clip_bundle", return_value=(None, None, None, "cpu")):
            with mock.patch("highlight_scorer.score_clip_exemplar", side_effect=hang):
                out = rank_shortlist_with_clip(
                    Path("/tmp/x.mp4"),
                    rows,
                    "pubg",
                    max_n=3,
                    timeout_sec=0.05,
                    min_score=0.1,
                )
        # Must return something usable (not hang / not empty).
        self.assertEqual(len(out), 3)


if __name__ == "__main__":
    unittest.main()
