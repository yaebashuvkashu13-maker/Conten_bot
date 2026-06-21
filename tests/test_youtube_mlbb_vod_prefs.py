#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from youtube_mlbb_vod_prefs import (  # noqa: E402
    build_vod_search_queries,
    passes_mlbb_vod_filters,
    rank_mlbb_vod_candidate,
    vod_youtube_duration_sp,
)


def _meta(title: str, *, uploader: str = "MLBB Ranked", duration: float = 780.0) -> dict:
    return {
        "id": "abc123",
        "title": title,
        "uploader": uploader,
        "duration": duration,
    }


def test_rejects_montage_and_tutorial() -> None:
    assert not passes_mlbb_vod_filters(_meta("MLBB Savage Montage Best Plays 2024"))
    assert not passes_mlbb_vod_filters(_meta("Mobile Legends Beginner Guide Mythic Rank"))
    assert not passes_mlbb_vod_filters(_meta("MLBB #shorts funny moments"))


def test_rejects_skin_showcase_titles() -> None:
    assert not passes_mlbb_vod_filters(_meta("MLBB Masha New Skin Showcase Season 41"))
    assert not passes_mlbb_vod_filters(_meta("Collector Skin Review Mobile Legends 2026"))
    assert not passes_mlbb_vod_filters(_meta("All New Skins Season 41 Battle Pass"))


def test_accepts_ranked_match_titles() -> None:
    assert passes_mlbb_vod_filters(_meta("MLBB Mythic Global Ranked Full Match Season 41"))
    assert passes_mlbb_vod_filters(_meta("Paquito vs Masha Mythic Ranked Match | MLBB"))
    assert passes_mlbb_vod_filters(_meta("Mobile Legends Legend Solo Queue Full Game Replay"))


def test_build_queries_include_global_hero_and_season() -> None:
    queries = build_vod_search_queries(season=41, heroes=("masha",), max_hero_queries=1)
    assert any("global" in q.lower() for q in queries)
    assert any("masha" in q.lower() for q in queries)
    assert any("season 41" in q.lower() for q in queries)
    assert all("minute" not in q.lower() for q in queries)


def test_rank_prefers_full_match_over_montage_hint() -> None:
    good = _meta("MLBB Mythic Global Ranked Full Match Gameplay Solo Queue", duration=780)
    weak = _meta("MLBB Mythic Ranked Match Gameplay Highlights", duration=780)
    assert rank_mlbb_vod_candidate(good) > rank_mlbb_vod_candidate(weak)


def test_rank_prefers_target_duration() -> None:
    ideal = _meta("MLBB Mythic Ranked Full Match", duration=780)
    longish = _meta("MLBB Mythic Ranked Full Match", duration=1140)
    assert rank_mlbb_vod_candidate(ideal) > rank_mlbb_vod_candidate(longish)


def test_youtube_duration_sp_enabled_by_default() -> None:
    assert vod_youtube_duration_sp({}) == "EgQQARgB"
    assert vod_youtube_duration_sp({"MLBB_VOD_YOUTUBE_DURATION_FILTER": "0"}) == ""
