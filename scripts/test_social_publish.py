#!/usr/bin/env python3
"""Unit tests for social_publish helpers (no network)."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import social_publish as sp  # noqa: E402


class SocialPublishTests(unittest.TestCase):
    def test_status_report_empty(self) -> None:
        report = sp.status_report({})
        self.assertTrue(report["enabled"])
        self.assertFalse(report["platforms"]["youtube"]["ready"])
        self.assertIn("GOOGLE_OAUTH", report["platforms"]["youtube"]["note"])

    def test_youtube_configured_with_refresh(self) -> None:
        env = {
            "GOOGLE_OAUTH_CLIENT_ID": "id",
            "GOOGLE_OAUTH_CLIENT_SECRET": "secret",
            "GOOGLE_OAUTH_REFRESH_TOKEN": "rt",
        }
        ok, note = sp.youtube_configured(env)
        self.assertTrue(ok)
        self.assertEqual(note, "ok")

    def test_platforms_keyboard(self) -> None:
        env = {
            "SOCIAL_PUBLISH_ENABLED": "1",
            "SOCIAL_VK_ENABLED": "0",
            "GOOGLE_OAUTH_CLIENT_ID": "id",
            "GOOGLE_OAUTH_CLIENT_SECRET": "secret",
            "GOOGLE_OAUTH_REFRESH_TOKEN": "rt",
        }
        kb = sp.platforms_keyboard("mlbb_vseg", "abc_1", env=env)
        flat = [b["callback_data"] for row in kb["inline_keyboard"] for b in row]
        self.assertIn("mlbb_vseg_pub:yt:abc_1", flat)
        self.assertIn("mlbb_vseg_social_back:abc_1", flat)
        self.assertTrue(any(c.startswith("mlbb_vseg_pub:ig:") for c in flat))

    def test_social_button_row(self) -> None:
        row = sp.social_button_row("pubg_vseg", "vid_9")
        self.assertEqual(row[0]["callback_data"], "pubg_vseg_social:vid_9")

    def test_short_maps(self) -> None:
        self.assertEqual(sp.SHORT_TO_PLATFORM["yt"], "youtube")
        self.assertEqual(sp.PLATFORM_SHORT["tiktok"], "tt")

    def test_append_publish_log(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sp.PUBLISH_LOG = Path(tmp) / "log.jsonl"
            sp.append_publish_log({"ok": True, "platform": "youtube"})
            text = sp.PUBLISH_LOG.read_text(encoding="utf-8")
            self.assertIn('"youtube"', text)


if __name__ == "__main__":
    raise SystemExit(unittest.main())
