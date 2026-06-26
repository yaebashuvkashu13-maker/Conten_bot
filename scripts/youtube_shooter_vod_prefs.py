#!/usr/bin/env python3
"""YouTube discovery for PUBG / Standoff ranked VOD segments (3–20 min window)."""

from __future__ import annotations

import os
import re
from urllib.parse import quote_plus

from youtube_mlbb_vod_prefs import (
    YOUTUBE_DURATION_SP_4_TO_20,
    YOUTUBE_FRESHNESS_SP_THIS_MONTH,
)

PUBG_TITLE_RE = re.compile(
    r"pubg|playerunknown|battlegrounds|metro\s*royale|пабг|метро\s*роял",
    re.I,
)
STANDOFF_TITLE_RE = re.compile(r"standoff\s*2|standoff2|стендоф", re.I)
LIVE_TITLE_RE = re.compile(r"🔴|\bLIVE\b|playoffs|grand finals", re.I)
BAD_TITLE_RE = re.compile(
    r"giveaway|#short\b|shorts\b|tiktok\b|montage|compilation|tutorial|"
    r"reaction|trailer|cinematic|aim\s*trainer|training\s*mode",
    re.I,
)

PUBG_CORE_QUERIES = (
    "PUBG Mobile ranked gameplay full match",
    "PUBG Mobile Metro Royale gameplay ranked",
    "PUBG Mobile squad fight ranked match replay",
    "PUBG Mobile TPP ranked full game no montage",
    "метро рояль пабг мобайл ранкед матч",
)

STANDOFF_CORE_QUERIES = (
    "Standoff 2 ranked gameplay full match",
    "Standoff 2 competitive gameplay replay",
    "Standoff 2 clutch ranked match",
    "Standoff 2 5v5 ranked gameplay",
)

PUBG_ANGLE_QUERIES = (
    "PUBG Mobile sniper ranked gameplay",
    "PUBG Mobile close range fight ranked",
    "PUBG Mobile final circle ranked match",
)

STANDOFF_ANGLE_QUERIES = (
    "Standoff 2 ace ranked gameplay",
    "Standoff 2 teamfight ranked replay",
)


def _queries_for(game: str) -> tuple[str, ...]:
    g = game.strip().lower()
    if g == "standoff":
        return STANDOFF_CORE_QUERIES + STANDOFF_ANGLE_QUERIES
    return PUBG_CORE_QUERIES + PUBG_ANGLE_QUERIES


def title_ok(game: str, title: str) -> bool:
    t = title or ""
    if LIVE_TITLE_RE.search(t) or BAD_TITLE_RE.search(t):
        return False
    g = game.strip().lower()
    if g == "standoff":
        return bool(STANDOFF_TITLE_RE.search(t))
    return bool(PUBG_TITLE_RE.search(t))


def vod_discovery_search_cycle(cycle: int, game: str, env: dict[str, str] | None = None) -> dict[str, object]:
    """Rotate shooter search queries (same batch/delay pattern as MLBB)."""
    env = env or {}
    queries = list(_queries_for(game))
    batch = int(env.get("MLBB_VOD_SEARCH_BATCH", env.get("SHOOTER_VOD_SEARCH_BATCH", "3")))
    delay = float(env.get("MLBB_VOD_SEARCH_DELAY", env.get("SHOOTER_VOD_SEARCH_DELAY", "6")))
    limit = int(env.get("MLBB_VOD_SEARCH_LIMIT", env.get("SHOOTER_VOD_SEARCH_LIMIT", "20")))
    if not queries:
        return {"queries": [], "batch": batch, "delay": delay, "limit": limit, "sp": ""}
    offset = (cycle * batch) % len(queries)
    picked: list[str] = []
    for i in range(batch):
        picked.append(queries[(offset + i) % len(queries)])
    sp = env.get("MLBB_VOD_YOUTUBE_DURATION_FILTER", "1") == "1"
    freshness = (
        YOUTUBE_FRESHNESS_SP_THIS_MONTH
        if env.get("MLBB_VOD_SEARCH_FRESH", "1") == "1"
        else ""
    )
    duration_sp = YOUTUBE_DURATION_SP_4_TO_20 if sp else ""
    search_urls = []
    for q in picked:
        url = f"ytsearch{limit}:{quote_plus(q)}"
        if duration_sp or freshness:
            url = f"https://www.youtube.com/results?search_query={quote_plus(q)}"
            if duration_sp:
                url += f"&sp={duration_sp}"
            elif freshness:
                url += f"&sp={freshness}"
        search_urls.append(url)
    return {
        "queries": picked,
        "urls": search_urls,
        "batch": batch,
        "delay": delay,
        "limit": limit,
        "cycle": cycle,
        "game": game,
    }
