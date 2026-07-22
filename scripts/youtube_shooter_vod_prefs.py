#!/usr/bin/env python3
"""YouTube discovery for PUBG Metro Royale / Standoff ranked VOD segments (3–20 min window)."""

from __future__ import annotations

import os
import re
from urllib.parse import quote_plus

from youtube_game_prefs import has_metro_royale
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
    r"обзор|гайд|guide|story|история|сюжет",
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
    r"\bstream\b|стрим|🔴|\bLIVE\b|босс|boss\s*drop|что\s+падает|сопровожден|"
    r"glitch|эксплойт|баг\b|exploring|exploration|сезон\s+(ид[её]т|coming)|"
    r"season\s+\d+\s+is\s+coming|coming\s+soon|update\s+preview|"
    r"loot\s*tour|туннел|tunnel\s+glitch|walkthrough|прохожден|"
    r"\bedit\b|edits\b|amv\b|music\s*video|recap\b",
    re.I,
)

FIGHT_TITLE_RE = re.compile(
    r"fight|бое[йв]|перестрел|штурм|clutch|ranked\s+match|full\s+match|"
    r"squad\s+fight|геймплей|gameplay|катк[аи]|рейд|pvp|перестрелк|"
    r"файт|штурмов",
    re.I,
)

PUBG_CORE_QUERIES = (
    "PUBG Mobile Metro Royale gameplay ranked fight",
    "PUBG Mobile Metro Royale full match squad fight",
    "PUBG Mobile Metro Royale TPP ranked replay",
    "метро рояль пабг мобайл ранкед матч перестрелка",
    "метро рояль пабг мобайл полный матч файт",
    "метро рояль пабг штурм катка",
    "пабг мобайл метро рояль геймплей бой",
    "метро рояль пабг стрим русский файт",
)

STANDOFF_CORE_QUERIES = (
    "Standoff 2 ranked gameplay full match",
    "Standoff 2 competitive gameplay replay",
    "Standoff 2 clutch ranked match",
    "Standoff 2 5v5 ranked gameplay",
)

PUBG_ANGLE_QUERIES = (
    "PUBG Mobile Metro Royale sniper fight",
    "PUBG Mobile Metro Royale close range fight",
    "PUBG Mobile Metro Royale final circle ranked",
    "метро рояль пабг файт перестрелка",
    "метро рояль пабг снайпер",
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
    g = game.strip().lower()
    if g == "pubg" and has_metro_royale({"title": t}):
        # RU Metro live VODs — allow «стрим» if title is clearly Metro Royale.
        if re.search(r"стрим|stream", t, re.I) and not LIVE_TITLE_RE.search(t):
            if CLASSIC_MODE_TITLE_RE.search(t) or METRO_VAGUE_TITLE_RE.search(t):
                return False
            return bool(PUBG_TITLE_RE.search(t))
    if LIVE_TITLE_RE.search(t) or BAD_TITLE_RE.search(t):
        return False
    if g == "standoff":
        return bool(STANDOFF_TITLE_RE.search(t))
    if g == "pubg":
        if CLASSIC_MODE_TITLE_RE.search(t) or METRO_VAGUE_TITLE_RE.search(t):
            return False
        return has_metro_royale({"title": t}) and bool(PUBG_TITLE_RE.search(t))
    return False


def rank_discovery_candidates(game: str, candidates: list[dict]) -> list[dict]:
    """Prefer Metro + Russian titles for PUBG discovery."""
    if not candidates:
        return candidates
    g = game.strip().lower()
    if g != "pubg" or os.environ.get("SHOOTER_VOD_PREFER_RUSSIAN", "1") != "1":
        return candidates
    from youtube_game_prefs import rank_candidate

    spec = {"require_metro_royale": True, "prefer_russian": True}
    return sorted(candidates, key=lambda row: -rank_candidate(row, spec))


def pick_discovery_candidate(game: str, candidates: list[dict]) -> dict | None:
    ranked = rank_discovery_candidates(game, candidates)
    if not ranked:
        return None

    def _dur(row: dict) -> float:
        try:
            return float(row.get("duration") or 0)
        except (TypeError, ValueError):
            return 0.0

    # Prefer 6–18 min VODs — long Metro streams waste hours downloading for one clip.
    prefer_max = float(os.environ.get("SHOOTER_VOD_PREFER_MAX_SEC", "1080"))
    prefer_min = float(os.environ.get("SHOOTER_VOD_PREFER_MIN_SEC", "360"))

    def _short_first(rows: list[dict]) -> list[dict]:
        sweet = [r for r in rows if prefer_min <= _dur(r) <= prefer_max]
        if sweet:
            return sorted(sweet, key=_dur)
        under = [r for r in rows if 0 < _dur(r) <= prefer_max]
        if under:
            return sorted(under, key=_dur)
        return rows

    def _fight_first(rows: list[dict]) -> list[dict]:
        fights = [r for r in rows if FIGHT_TITLE_RE.search(str(r.get("title") or ""))]
        return fights + [r for r in rows if r not in fights]

    if game.strip().lower() == "pubg":
        from pubg_metro_royale_gate import title_metro_hint
        from youtube_game_prefs import russian_score

        pool = _fight_first(_short_first(ranked[:20]))
        for cand in pool[:12]:
            title = str(cand.get("title") or "")
            if title_metro_hint(title) and russian_score(cand) >= 0.06:
                return cand
        for cand in pool[:12]:
            if title_metro_hint(str(cand.get("title") or "")):
                return cand
        for cand in pool[:8]:
            if russian_score(cand) >= 0.12:
                return cand
        return pool[0]
    return _fight_first(_short_first(ranked))[0]


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
