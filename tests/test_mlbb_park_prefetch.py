#!/usr/bin/env python3
"""Park-dead VODs must not be re-downloaded / counted as pickable prefetch fuel."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))


def test_parked_ids_blocked_from_reuse(tmp_path, monkeypatch) -> None:
    import mlbb_vod_segment_feed as feed

    park = tmp_path / "park_dead"
    park.mkdir()
    (park / "yt_JunkParked0.mp4").write_bytes(b"x" * 100)
    monkeypatch.setattr(feed, "PARK_DEAD", park)
    monkeypatch.setenv("MLBB_VOD_DISCOVERY_REUSE_ZERO_SEND", "1")
    monkeypatch.setenv("MLBB_VOD_DISCOVERY_REUSE_AFTER_MISS", "1")

    with (
        patch.object(feed, "_discovery_starvation_level", return_value=99),
        patch.object(
            feed,
            "_load_state",
            return_value={
                "zero_send_youtube_ids": ["JunkParked0", "FreshZeroSe"],
                "vods": [],
            },
        ),
    ):
        used = {"JunkParked0", "FreshZeroSe", "OtherUsedId"}
        effective = feed._discovery_effective_used(used)

    assert "JunkParked0" in effective  # stay blocked
    assert "FreshZeroSe" not in effective  # reusable when starving


def test_should_prefetch_respects_inbox_cap(tmp_path, monkeypatch) -> None:
    import mlbb_vod_segment_feed as feed

    inbox = tmp_path / "inbox"
    inbox.mkdir()
    monkeypatch.setattr(feed, "INBOX", inbox)
    monkeypatch.setenv("MLBB_VOD_PREFETCH", "1")
    monkeypatch.setenv("MLBB_VOD_INBOX_MAX", "2")

    for name in ("yt_AAAA1111111.mp4", "yt_BBBB2222222.mp4"):
        (inbox / name).write_bytes(b"x" * 2_000_000)

    with patch.object(feed, "_inbox_pickable_count", return_value=0):
        assert feed._should_prefetch_download([]) is False

    (inbox / "yt_BBBB2222222.mp4").unlink()
    with patch.object(feed, "_inbox_pickable_count", return_value=0):
        assert feed._should_prefetch_download([]) is True
    with patch.object(feed, "_inbox_pickable_count", return_value=2):
        assert feed._should_prefetch_download([]) is False


def test_repark_moves_inbox_copies(tmp_path, monkeypatch) -> None:
    import mlbb_vod_segment_feed as feed

    inbox = tmp_path / "inbox"
    park = tmp_path / "park_dead"
    inbox.mkdir()
    park.mkdir()
    monkeypatch.setattr(feed, "INBOX", inbox)
    monkeypatch.setattr(feed, "PARK_DEAD", park)
    (park / "yt_DeadAgain12.mp4").write_bytes(b"old")
    copy = inbox / "yt_DeadAgain12.mp4"
    copy.write_bytes(b"x" * 2_000_000)
    assert feed._repark_known_dead_inbox() == 1
    assert not copy.exists()
