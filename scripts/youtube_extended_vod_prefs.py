#!/usr/bin/env python3
"""YouTube discovery for Genshin / WoT VOD segments (4–20 min window)."""

from __future__ import annotations

import os
import re
from urllib.parse import quote_plus

from youtube_mlbb_vod_prefs import (
    YOUTUBE_DURATION_SP_4_TO_20,
    YOUTUBE_FRESHNESS_SP_THIS_MONTH,
)
from youtube_shooter_vod_prefs import BAD_TITLE_RE, LIVE_TITLE_RE

GENSHIN_TITLE_RE = re.compile(r"genshin|геншин|原神", re.I)
WOT_TITLE_RE = re.compile(
    r"world\s+of\s+tanks|wot\s*blitz|tanks?\s*blitz|танк|blitz",
    re.I,
)
GENSHIN_BAD_TITLE_RE = re.compile(
    r"banner|wish|gacha|обзор\s*персонаж|build\s*guide|tier\s*list|story\s*quest",
    re.I,
)
WOT_BAD_TITLE_RE = re.compile(
    r"premium\s*shop|giveaway|обзор\s*танка|guide|гайд|grind\s*guide",
    re.I,
)

GENSHIN_CORE_QUERIES = (
    "Genshin Impact boss fight full gameplay",
    "Genshin Impact spiral abyss boss floor gameplay",
    "Genshin Impact weekly boss fight replay",
    "геншин импакт босс файт полный геймплей",
    "геншин импакт домен босс матч",
)

WOT_CORE_QUERIES = (
    "World of Tanks Blitz ranked gameplay full match",
    "WoT Blitz epic frag gameplay replay",
    "World of Tanks Blitz battle highlights gameplay",
    "танки блиц ранкед матч геймплей",
    "world of tanks blitz фраг перестрелка",
)

GENSHIN_ANGLE_QUERIES = (
    "Genshin Impact raid boss co-op fight",
    "Genshin Impact boss rush gameplay",
)

WOT_ANGLE_QUERIES = (
    "WoT Blitz clutch 1v3 gameplay",
    "World of Tanks Blitz tournament fight replay",
)


def _queries_for(game: str) -> tuple[str, ...]:
    g = game.strip().lower()
    if g == "wot":
        return WOT_CORE_QUERIES + WOT_ANGLE_QUERIES
    return GENSHIN_CORE_QUERIES + GENSHIN_ANGLE_QUERIES


def title_ok(game: str, title: str) -> bool:
    t = title or ""
    if LIVE_TITLE_RE.search(t) or BAD_TITLE_RE.search(t):
        return False
    g = game.strip().lower()
    if g == "genshin":
        if GENSHIN_BAD_TITLE_RE.search(t):
            return False
        return bool(GENSHIN_TITLE_RE.search(t))
    if g == "wot":
        if WOT_BAD_TITLE_RE.search(t):
            return False
        return bool(WOT_TITLE_RE.search(t))
    return False


def vod_discovery_search_cycle(cycle: int, game: str, env: dict[str, str] | None = None) -> dict[str, object]:
    """Rotate extended-game queries + YouTube filters (freshness / duration / week)."""
    env = env or {}
    queries = list(_queries_for(game))
    batch = int(env.get("EXTENDED_VOD_SEARCH_BATCH", env.get("SHOOTER_VOD_SEARCH_BATCH", "3")))
    delay = float(env.get("EXTENDED_VOD_SEARCH_DELAY", env.get("SHOOTER_VOD_SEARCH_DELAY", "6")))
    limit = int(env.get("EXTENDED_VOD_SEARCH_LIMIT", env.get("SHOOTER_VOD_SEARCH_LIMIT", "20")))
    if not queries:
        return {"queries": [], "batch": batch, "delay": delay, "limit": limit, "sp": ""}
    offset = (cycle * batch) % len(queries)
    picked = [queries[(offset + i) % len(queries)] for i in range(batch)]
    mode = int(cycle) % 3
    use_duration = env.get("MLBB_VOD_YOUTUBE_DURATION_FILTER", "1") == "1"
    use_fresh = env.get("MLBB_VOD_SEARCH_FRESH", "1") == "1"
    if mode == 0 and use_fresh:
        sp = YOUTUBE_FRESHNESS_SP_THIS_MONTH
        filter_mode = "fresh_month"
    elif mode == 1 and use_duration:
        sp = YOUTUBE_DURATION_SP_4_TO_20
        filter_mode = "duration_4_20"
    elif use_fresh:
        from youtube_mlbb_vod_prefs import YOUTUBE_FRESHNESS_SP_THIS_WEEK

        sp = YOUTUBE_FRESHNESS_SP_THIS_WEEK
        filter_mode = "fresh_week"
    elif use_duration:
        sp = YOUTUBE_DURATION_SP_4_TO_20
        filter_mode = "duration_4_20"
    else:
        sp = ""
        filter_mode = "ytsearch"
    search_urls = []
    for q in picked:
        if sp:
            url = f"https://www.youtube.com/results?search_query={quote_plus(q)}&sp={sp}"
        else:
            url = f"ytsearch{limit}:{q}"
        search_urls.append(url)
    return {
        "queries": picked,
        "urls": search_urls,
        "batch": batch,
        "delay": delay,
        "limit": limit,
        "cycle": cycle,
        "game": game,
        "filter_mode": filter_mode,
        "sp": sp,
    }
