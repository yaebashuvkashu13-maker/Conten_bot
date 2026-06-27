#!/usr/bin/env python3
"""YouTube discovery for PUBG Metro Royale / Standoff ranked VOD segments (3–20 min window)."""

from __future__ import annotations

import os
import re
from urllib.parse import quote_plus

from youtube_game_prefs import has_metro_royale, rank_candidate
from youtube_mlbb_vod_prefs import (
    YOUTUBE_DURATION_SP_4_TO_20,
    YOUTUBE_FRESHNESS_SP_THIS_MONTH,
)

PUBG_TITLE_RE = re.compile(
    r"pubg|playerunknown|battlegrounds|metro\s*royale|пабг|метро\s*роял",
    re.I,
)
METRO_VAGUE_TITLE_RE = re.compile(
    r"gone without|without a trace|tips\s+and\s+tricks|highlights?|"
    r"обзор|гайд|guide|story|история|сюжет|ивент|event|royal\s+pass|"
    r"проблем|problem|clash:|турнир|championship",
    re.I,
)
CLASSIC_MODE_TITLE_RE = re.compile(
    r"\b(erangel|livik|sanhok|miramar|vikendi|classic\s+mode|"
    r"ranked\s+classic|эрангель|ливик)\b",
    re.I,
)
STANDOFF_TITLE_RE = re.compile(r"standoff\s*2|standoff2|стендоф", re.I)
LIVE_TITLE_RE = re.compile(r"🔴|\bLIVE\b|playoffs|grand finals", re.I)
BAD_TITLE_RE = re.compile(
    r"giveaway|#short\b|shorts\b|tiktok\b|montage|compilation|tutorial|"
    r"reaction|trailer|cinematic|aim\s*trainer|training\s*mode|"
    r"highlight|highlights|хайлайт|tips\s+and\s+tricks|tips\s*&\s*tricks|"
    r"guide|обзор|trick|совет|"
    r"\bstream\b|стрим|🔴|\bLIVE\b|босс|boss\s*drop|что\s+падает|сопровожден",
    re.I,
)

# Russian-first: gameplay / fight / ranked match (no «стрим» — blocked by BAD_TITLE_RE).
PUBG_RU_CORE_QUERIES = (
    "метро рояль пабг мобайл геймплей ранкед",
    "метро рояль пабг мобайл полный матч",
    "метро рояль пабг мобайл перестрелка",
    "метро рояль пабг мобайл отряд ранкед",
    "пабг метро рояль геймплей матч",
    "пабг мобайл метро рояль TPP ранкед",
    "пабг метро рояль ранкед матч геймплей",
    "метро рояль пабг мобайл на русском геймплей",
    "PUBG Mobile Metro Royale геймплей русский",
    "PUBG Mobile Metro Royale перестрелка ranked",
)

PUBG_RU_ANGLE_QUERIES = (
    "метро рояль пабг снайпер перестрелка",
    "метро рояль пабг финальный круг",
    "метро рояль пабг ближний бой",
    "метро рояль пабг эвакуация геймплей",
    "метро рояль пабг лут и перестрелка",
    "пабг метро рояль кладбище босса",
    "пабг метро рояль clutch ranked",
    "метро рояль пабг соло против отряда",
    "PUBG Metro Royale squad fight ranked replay",
    "PUBG Mobile Metro Royale close range fight",
)

PUBG_EN_CORE_QUERIES = (
    "PUBG Mobile Metro Royale gameplay ranked",
    "PUBG Mobile Metro Royale full match",
    "PUBG Mobile Metro Royale squad fight ranked",
)

STANDOFF_CORE_QUERIES = (
    "Standoff 2 ranked gameplay full match",
    "Standoff 2 competitive gameplay replay",
    "Standoff 2 clutch ranked match",
    "Standoff 2 5v5 ranked gameplay",
)

STANDOFF_ANGLE_QUERIES = (
    "Standoff 2 ace ranked gameplay",
    "Standoff 2 teamfight ranked replay",
)


def default_pubg_vod_search_queries() -> tuple[str, ...]:
    return PUBG_RU_CORE_QUERIES + PUBG_RU_ANGLE_QUERIES + PUBG_EN_CORE_QUERIES


def default_pubg_vod_search_queries_csv() -> str:
    return ",".join(default_pubg_vod_search_queries())


def _parse_query_csv(raw: str) -> tuple[str, ...]:
    out: list[str] = []
    for part in raw.split(","):
        q = part.strip()
        if q and q not in out:
            out.append(q)
    return tuple(out)


def _queries_for(game: str, env: dict[str, str] | None = None) -> tuple[str, ...]:
    g = game.strip().lower()
    env = env or {}
    if g == "standoff":
        return STANDOFF_CORE_QUERIES + STANDOFF_ANGLE_QUERIES
    override = env.get("PUBG_VOD_SEARCH_QUERIES", "").strip()
    if override:
        parsed = _parse_query_csv(override)
        if parsed:
            return parsed
    return default_pubg_vod_search_queries()


def title_ok(game: str, title: str) -> bool:
    t = title or ""
    if LIVE_TITLE_RE.search(t) or BAD_TITLE_RE.search(t):
        return False
    g = game.strip().lower()
    if g == "standoff":
        return bool(STANDOFF_TITLE_RE.search(t))
    if g == "pubg":
        if CLASSIC_MODE_TITLE_RE.search(t) or METRO_VAGUE_TITLE_RE.search(t):
            return False
        return has_metro_royale({"title": t}) and bool(PUBG_TITLE_RE.search(t))
    return False


def rank_pubg_candidate(meta: dict) -> float:
    """Higher = prefer RU Metro gameplay VODs in discovery pool."""
    score = rank_candidate(meta, {"require_metro_royale": True, "prefer_russian": True})
    dur = float(meta.get("duration") or 0)
    if 240 <= dur <= 1200:
        score += 2.0
    title = (meta.get("title") or "").lower()
    if re.search(r"перестрелк|fight|clutch|kill|ранкед|ranked|squad|отряд", title, re.I):
        score += 1.5
    if re.search(r"обзор|гайд|tips|guide|event|ивент|pass", title, re.I):
        score -= 4.0
    return score


def vod_discovery_search_cycle(cycle: int, game: str, env: dict[str, str] | None = None) -> dict[str, object]:
    """Rotate shooter search queries (same batch/delay pattern as MLBB)."""
    env = env or {}
    queries = list(_queries_for(game, env))
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
