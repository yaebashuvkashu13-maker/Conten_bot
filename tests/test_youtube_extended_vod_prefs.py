"""Tests for Genshin / WoT YouTube VOD discovery prefs."""

from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

from youtube_extended_vod_prefs import (  # noqa: E402
    rank_wot_vod_candidate,
    title_ok,
    vod_discovery_search_cycle,
    wot_recommended_duration_window,
    wot_target_duration_sec,
)


def test_genshin_title_ok() -> None:
    assert title_ok("genshin", "Genshin Impact boss fight gameplay")
    assert not title_ok("genshin", "Genshin wish banner guide")


def test_wot_title_ok() -> None:
    assert title_ok("wot", "WoT Blitz ranked frag gameplay")
    assert not title_ok("wot", "premium shop giveaway tanks")
    assert not title_ok("wot", "🔴 WOT BLITZ LIVE stream chill chatting")
    assert not title_ok("wot", "Blitz Summer Cup Grand Final NA")


def test_wot_prefers_match_over_stream() -> None:
    match = {
        "title": "WoT Blitz ranked full match gameplay no commentary",
        "duration": 420,
        "upload_date": "20260718",
    }
    stream = {
        "title": "🔴 Chill WoT Blitz stream chatting live",
        "duration": 7200,
        "upload_date": "20260718",
    }
    assert rank_wot_vod_candidate(match) > rank_wot_vod_candidate(stream)


def test_wot_duration_window_from_bank() -> None:
    min_sec, max_sec = wot_recommended_duration_window()
    assert min_sec == 120
    assert max_sec == 1500
    assert wot_target_duration_sec() == 390


def test_discovery_cycle_includes_wot_queries() -> None:
    params = vod_discovery_search_cycle(0, "wot", {})
    assert params["queries"]
    assert params["urls"]
    assert "ranked" in params["queries"][0].lower() or "blitz" in params["queries"][0].lower()
