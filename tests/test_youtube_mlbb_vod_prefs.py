#!/usr/bin/env python3
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from youtube_mlbb_vod_prefs import (  # noqa: E402
    build_vod_search_queries,
    passes_mlbb_vod_filters,
    passes_upload_freshness,
    rank_mlbb_vod_candidate,
    upload_age_days,
    vod_search_date_sort,
    vod_youtube_duration_sp,
    vod_youtube_freshness_sp,
)


def _meta(title: str, *, uploader: str = "MLBB Ranked", duration: float = 780.0, upload_date: str = "") -> dict:
    return {
        "id": "abc123",
        "title": title,
        "uploader": uploader,
        "duration": duration,
        "upload_date": upload_date,
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


def test_build_queries_core_first_supplements_optional() -> None:
    queries = build_vod_search_queries(season=41, heroes=("masha",), max_hero_queries=1)
    assert queries[0] == "MLBB mythic ranked full match gameplay"
    assert "MLBB mythic masha ranked gameplay" in queries
    assert queries[-1] == "MLBB mythic global masha season 41 ranked gameplay"
    assert all("minute" not in q.lower() for q in queries)

    no_sup = build_vod_search_queries(season=41, include_supplements=False, max_hero_queries=1)
    assert all("season 41" not in q for q in no_sup)
    assert any("masha" in q for q in no_sup)


def test_upload_freshness_filter() -> None:
    now = datetime(2026, 6, 21, tzinfo=timezone.utc)
    fresh = _meta("MLBB Mythic Ranked", upload_date="20260619")
    stale = _meta("MLBB Mythic Ranked", upload_date="20260101")
    assert upload_age_days("20260619", now=now) == 2
    assert passes_upload_freshness(fresh, max_age_days=21)
    assert not passes_upload_freshness(stale, max_age_days=21)


def test_rank_prefers_fresh_upload() -> None:
    today = datetime.now(timezone.utc).date()
    fresh_day = (today - timedelta(days=2)).strftime("%Y%m%d")
    old_day = (today - timedelta(days=40)).strftime("%Y%m%d")
    fresh = _meta("MLBB Mythic Ranked Full Match", duration=780, upload_date=fresh_day)
    old = _meta("MLBB Mythic Ranked Full Match", duration=780, upload_date=old_day)
    assert rank_mlbb_vod_candidate(fresh) > rank_mlbb_vod_candidate(old)


def test_rank_prefers_full_match_over_montage_hint() -> None:
    good = _meta("MLBB Mythic Global Ranked Full Match Gameplay Solo Queue", duration=780)
    weak = _meta("MLBB Mythic Ranked Match Gameplay Highlights", duration=780)
    assert rank_mlbb_vod_candidate(good) > rank_mlbb_vod_candidate(weak)


def test_fresh_search_uses_month_sp_not_duration_sp() -> None:
    assert vod_search_date_sort({"MLBB_VOD_SEARCH_FRESH": "1"}) is True
    assert vod_youtube_freshness_sp({"MLBB_VOD_SEARCH_FRESH": "1"}) == "EgQIBBAB"
    assert vod_youtube_duration_sp({"MLBB_VOD_SEARCH_FRESH": "1"}) == ""
    assert vod_youtube_duration_sp({"MLBB_VOD_SEARCH_FRESH": "0"}) == "EgQQARgB"
