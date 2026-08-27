#!/usr/bin/env python3
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from youtube_mlbb_vod_prefs import (  # noqa: E402
    build_vod_search_queries,
    passes_mlbb_game_title,
    passes_mlbb_vod_filters,
    passes_upload_freshness,
    pick_vod_search_batch,
    rank_mlbb_vod_candidate,
    upload_age_days,
    vod_discovery_search_cycle,
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


def test_rejects_guide_listicle_titles() -> None:
    assert not passes_mlbb_vod_filters(_meta("BEST SOLO CARRY Heroes For Every Role | Season 41 ~ Mobile Legends"))
    assert not passes_mlbb_game_title("BEST SOLO CARRY Heroes For Every Role | Season 41")


def test_accepts_ranked_match_titles() -> None:
    assert passes_mlbb_vod_filters(_meta("MLBB Mythic Global Ranked Full Match Season 41"))
    assert passes_mlbb_vod_filters(_meta("Paquito vs Masha Mythic Ranked Match | MLBB"))
    assert passes_mlbb_vod_filters(_meta("Mobile Legends Legend Solo Queue Full Game Replay"))


def test_accepts_implicit_mlbb_ranked_titles() -> None:
    assert passes_mlbb_game_title("MID Hayabusa Full Highlights Ranked Game Mythical Glory")
    assert passes_mlbb_vod_filters(_meta("OBISIDIA DESTROYS RANKED MATCH MYTHIC Gameplay"))
    assert passes_mlbb_vod_filters(_meta("WanWan MVP Mythic Glory Ranked Gameplay | Mobile Legends"))


def test_build_queries_returns_twenty() -> None:
    queries = build_vod_search_queries(season=41)
    assert len(queries) == 20
    assert queries[0] == "MLBB mythic ranked full match gameplay"
    assert any("masha" in q for q in queries)
    assert any("placement" in q for q in queries)
    assert all("minute" not in q.lower() for q in queries)


def test_pick_vod_search_batch_rotates() -> None:
    queries = ["a", "b", "c", "d", "e"]
    batch1, off1 = pick_vod_search_batch(queries, 0, 2)
    batch2, off2 = pick_vod_search_batch(queries, off1, 2)
    assert batch1 == ["a", "b"]
    assert batch2 == ["c", "d"]
    assert off2 == 4


def test_discovery_search_cycle_rotates_modes() -> None:
    m0 = vod_discovery_search_cycle(0)
    m1 = vod_discovery_search_cycle(1)
    m2 = vod_discovery_search_cycle(2)
    assert m0["youtube_search_date"] is True
    assert m1["youtube_search_date"] is False
    assert m1["youtube_duration_sp"]
    assert m2["youtube_freshness_sp"] == "EgQIARAB"


def test_upload_freshness_filter() -> None:
    now = datetime.now(timezone.utc)
    fresh_day = (now - timedelta(days=2)).strftime("%Y%m%d")
    stale_day = (now - timedelta(days=40)).strftime("%Y%m%d")
    fresh = _meta("MLBB Mythic Ranked", upload_date=fresh_day)
    stale = _meta("MLBB Mythic Ranked", upload_date=stale_day)
    assert upload_age_days(fresh_day, now=now) == 2
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


def test_rank_prefers_kill_heavy_title() -> None:
    fight = _meta("MLBB Mythic Savage Teamfight 22 Kills MVP Ranked Full Match", duration=780)
    passive = _meta("MLBB Mythic Ranked Full Match Macro Farm Gameplay", duration=780)
    assert rank_mlbb_vod_candidate(fight) > rank_mlbb_vod_candidate(passive)


def test_build_queries_includes_combat_angle() -> None:
    queries = build_vod_search_queries(season=41)
    lowered = [q.lower() for q in queries]
    assert any("double kill" in q for q in lowered)
    assert any("savage" in q or "maniac" in q for q in lowered)


def test_fresh_search_uses_month_sp_not_duration_sp() -> None:
    assert vod_search_date_sort({"MLBB_VOD_SEARCH_FRESH": "1"}) is True
    assert vod_youtube_freshness_sp({"MLBB_VOD_SEARCH_FRESH": "1"}) == "EgQIBBAB"
    assert vod_youtube_duration_sp({"MLBB_VOD_SEARCH_FRESH": "1"}) == ""
    assert vod_youtube_duration_sp({"MLBB_VOD_SEARCH_FRESH": "0"}) == "EgQQARgB"
