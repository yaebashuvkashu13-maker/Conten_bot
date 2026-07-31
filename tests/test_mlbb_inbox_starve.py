#!/usr/bin/env python3
"""Inbox with only exhausted mp4s must download / force-revive — not spin forever."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))


def test_inbox_pickable_skips_exhausted(tmp_path, monkeypatch) -> None:
    import mlbb_vod_segment_feed as feed

    inbox = tmp_path / "inbox"
    inbox.mkdir()
    monkeypatch.setattr(feed, "INBOX", inbox)
    good = inbox / "yt_GOOD1234567.mp4"
    dead = inbox / "yt_DEAD1234567.mp4"
    good.write_bytes(b"x" * 2_000_000)
    dead.write_bytes(b"x" * 2_000_000)
    registry = [
        {"id": "GOOD1234567", "path": str(good), "exhausted": False},
        {"id": "DEAD1234567", "path": str(dead), "exhausted": True},
    ]
    assert feed._inbox_pickable_count(registry) == 1
    registry[0]["exhausted"] = True
    assert feed._inbox_pickable_count(registry) == 0


def test_force_revive_ignores_starvation(monkeypatch) -> None:
    import mlbb_vod_segment_feed as feed

    monkeypatch.setenv("MLBB_VOD_REVIVE_TITLE", "1")
    registry = [
        {
            "id": "Abcdefghijk",
            "path": "/tmp/missing.mp4",
            "exhausted": True,
            "title": "SAVAGE + MANIAC gameplay 20 kills",
            "revive_count": 0,
        }
    ]
    # Missing file → 0
    with patch.object(feed, "_discovery_starvation_level", return_value=0):
        assert feed._revive_exhausted_inbox_candidates(registry, force=True) == 0

    # Existing inbox path + force → revive even at starvation 0
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "yt_Abcdefghijk.mp4"
        path.write_bytes(b"x" * 2_000_000)
        registry[0]["path"] = str(path)
        with (
            patch.object(feed, "_discovery_starvation_level", return_value=0),
            patch.object(feed, "_load_state", return_value={"vods": registry}),
            patch.object(feed, "_save_state"),
        ):
            n = feed._revive_exhausted_inbox_candidates(registry, force=True)
        assert n == 1
        assert registry[0]["exhausted"] is False


def test_revive_skips_park_dead(monkeypatch, tmp_path) -> None:
    import mlbb_vod_segment_feed as feed

    monkeypatch.setenv("MLBB_VOD_REVIVE_TITLE", "1")
    park = tmp_path / "park_dead"
    park.mkdir()
    path = park / "yt_ParkedKillzzz.mp4"
    path.write_bytes(b"x" * 2_000_000)
    registry = [
        {
            "id": "ParkedKillzzz",
            "path": str(path),
            "exhausted": True,
            "title": "triple kill savage maniac",
            "revive_count": 0,
        }
    ]
    with patch.object(feed, "_discovery_starvation_level", return_value=99):
        assert feed._revive_exhausted_inbox_candidates(registry, force=True) == 0
