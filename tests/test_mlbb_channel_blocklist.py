"""Blocked YouTube channels for MLBB Shorts ingest / feed."""

from __future__ import annotations

from mlbb_channel_blocklist import (
    filter_channel_feeds,
    is_blocked_candidate,
    is_blocked_feed_url,
    matches_blocked_channel,
)


def test_jess_no_limit_blocked_by_default() -> None:
    assert matches_blocked_channel("Jess No Limit")
    assert matches_blocked_channel("https://www.youtube.com/@JessNoLimit/shorts")
    assert is_blocked_feed_url("https://www.youtube.com/@JessNoLimit/videos")


def test_filter_channel_feeds_removes_blocked() -> None:
    feeds = [
        "https://www.youtube.com/@Betosky/shorts",
        "https://www.youtube.com/@JessNoLimit/shorts",
        "https://www.youtube.com/@akosidogie/shorts",
    ]
    assert filter_channel_feeds(feeds) == [
        "https://www.youtube.com/@Betosky/shorts",
        "https://www.youtube.com/@akosidogie/shorts",
    ]


def test_is_blocked_candidate_channel_field() -> None:
    blocked, reason = is_blocked_candidate(
        {"video_id": "JHN9vZFwZq8", "channel": "Jess No Limit", "title": "some gameplay"}
    )
    assert blocked
    assert reason == "blocked_channel:channel"


def test_skin_review_title_not_blocked_without_channel() -> None:
    blocked, _ = is_blocked_candidate(
        {"video_id": "abc12345678", "title": "REVIEW SKIN EPIC TERBARU MARTIS"}
    )
    assert not blocked
