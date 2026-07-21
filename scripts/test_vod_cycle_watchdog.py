#!/usr/bin/env python3
"""Tests for vod_cycle_watchdog zero-send loop detection."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import vod_cycle_watchdog as wdg  # noqa: E402


class VodCycleWatchdogTests(unittest.TestCase):
    def test_zero_send_loop_detected(self) -> None:
        lines = [
            "2026-07-21 04:00:00 INFO zero send — keep vod=yt_A.mp4 for retry (presend/soften) streak=9",
            "2026-07-21 04:06:00 INFO zero send — keep vod=yt_A.mp4 for retry (presend/soften) streak=10",
        ]
        ok, detail = wdg._zero_send_loop(lines)
        self.assertTrue(ok)
        self.assertIn("yt_A.mp4", detail)

    def test_zero_send_loop_different_vods(self) -> None:
        lines = [
            "zero send — keep vod=yt_A.mp4 for retry (presend/soften) streak=12",
            "zero send — keep vod=yt_B.mp4 for retry (presend/soften) streak=3",
        ]
        ok, _ = wdg._zero_send_loop(lines)
        self.assertFalse(ok)


if __name__ == "__main__":
    raise SystemExit(unittest.main())
