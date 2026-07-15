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
    r"обзор|гайд|guide|story|история|сюжет|"
    r"shop|магазин|black\s*market|чёрн\w*\s*рын|скам|scam|обмен|"
    r"going to remove|уберут\s+метро|release\s*date|дата\s*выхода|"
    r"honor\s*rewards|награды|rewards?|problem|проблема|"
    r"flying\s+enemy|ammo\s*level|what+|\?\?+|is\s+pubg\s+going",
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
    r"\btips?\b|гайд|guide|обзор|trick|совет|begginers?|beginners?|"
    r"sensitivity|чувствительност|how\s+to\s+get|мем|memes?|"
    r"dual\s*mode|1v5\s*comeback|nonstop\s*killing|"
    r"\bstream\b|стрим|🔴|\bLIVE\b|boss\s*drop|что\s+падает|сопровожден",
    re.I,
)
COMBAT_TITLE_RE = re.compile(
    r"full\s*match|полный\s*матч|gameplay|геймплей|ranked|ранкед|"
    r"fight|перестрелка|clutch|сквад|squad|vs\s*squad|против\s*сква|"
    r"соло|solo|рейд|raid|файт|tpp|эвакуац",
    re.I,
)

PUBG_CORE_QUERIES = (
    "PUBG Mobile Metro Royale full match gameplay",
    "PUBG Mobile Metro Royale ranked squad fight",
    "PUBG Mobile Metro Royale TPP ranked full match",
    "метро рояль пабг мобайл полный матч геймплей",
    "метро рояль пабг мобайл ранкед перестрелка",
    "метро рояль пабг мобайл стрим полный матч",
    "пабг мобайл метро рояль геймплей файт",
    "метро рояль пабг соло против сквада",
    "PUBG Mobile Metro Royale solo vs squad ranked",
    "метро рояль пабг стрим русский геймплей",
)

STANDOFF_CORE_QUERIES = (
    "Standoff 2 ranked gameplay full match",
    "Standoff 2 competitive gameplay replay",
    "Standoff 2 clutch ranked match",
    "Standoff 2 5v5 ranked gameplay",
    "стендофф 2 ранкед полный матч геймплей",
    "стендоф 2 соревновательный матч полный",
    "standoff 2 ранкед катка полный матч",
    "стендофф 2 ранг геймплей без монтажа",
    "Standoff 2 ranked full game no montage",
    "стендофф 2 5на5 ранкед катка",
)

PUBG_ANGLE_QUERIES = (
    "PUBG Mobile Metro Royale sniper fight ranked",
    "PUBG Mobile Metro Royale close range fight",
    "PUBG Mobile Metro Royale final circle ranked",
    "метро рояль пабг файт перестрелка ранкед",
    "метро рояль пабг снайпер геймплей",
)

STANDOFF_ANGLE_QUERIES = (
    "Standoff 2 ace ranked gameplay",
    "Standoff 2 teamfight ranked replay",
    "стендофф 2 эйс ранкед геймплей",
    "стендофф 2 клатч ранкед полный",
    "стендоф 2 тимфайт ранкед катка",
    "Standoff 2 dust sandstone ranked full match",
    "стендофф 2 сандстоун ранкед матч",
    "standoff 2 competitive match russian",
)


def _env_query_list(*keys: str) -> list[str]:
    for key in keys:
        raw = (os.environ.get(key) or "").strip().strip('"').strip("'")
        if not raw:
            continue
        return [q.strip() for q in raw.split(",") if q.strip()]
    return []


def _queries_for(game: str) -> tuple[str, ...]:
    g = game.strip().lower()
    if g == "standoff":
        override = _env_query_list("STANDOFF_VOD_SEARCH_QUERIES", "SHOOTER_STANDOFF_SEARCH_QUERIES")
        return tuple(override) if override else STANDOFF_CORE_QUERIES + STANDOFF_ANGLE_QUERIES
    override = _env_query_list("PUBG_VOD_SEARCH_QUERIES", "SHOOTER_PUBG_SEARCH_QUERIES")
    return tuple(override) if override else PUBG_CORE_QUERIES + PUBG_ANGLE_QUERIES


def _pref_min_sec() -> float:
    return float(os.environ.get("SHOOTER_VOD_PREF_MIN_SEC", "600"))


def title_ok(game: str, title: str) -> bool:
    t = title or ""
    g = game.strip().lower()
    if g == "pubg" and has_metro_royale({"title": t}):
        # RU Metro live VODs — allow «стрим» if title is clearly Metro Royale.
        if re.search(r"стрим|stream", t, re.I) and not LIVE_TITLE_RE.search(t):
            if CLASSIC_MODE_TITLE_RE.search(t) or METRO_VAGUE_TITLE_RE.search(t):
                return False
            return bool(PUBG_TITLE_RE.search(t))
    if g == "standoff" and STANDOFF_TITLE_RE.search(t):
        # Allow RU ranked streams / full matches; still reject tip/guide junk.
        if re.search(r"стрим|stream", t, re.I) and not LIVE_TITLE_RE.search(t):
            junk = re.compile(
                r"\btips?\b|гайд|guide|обзор|sensitivity|чувствительност|"
                r"begginers?|beginners?|мем|memes?|dual\s*mode|"
                r"montage|compilation|shorts?\b|#short\b",
                re.I,
            )
            if junk.search(t):
                return False
            return True
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
    """Prefer Russian + mid-length combat VODs for shooter discovery."""
    if not candidates:
        return candidates
    g = game.strip().lower()
    if g not in ("pubg", "standoff") or os.environ.get("SHOOTER_VOD_PREFER_RUSSIAN", "1") != "1":
        return candidates
    from youtube_game_prefs import rank_candidate

    spec = {
        "require_metro_royale": g == "pubg",
        "prefer_russian": True,
        "ideal_duration_sec": float(os.environ.get("SHOOTER_VOD_IDEAL_SEC", "900")),
    }
    pref = _pref_min_sec()

    def _score(row: dict) -> float:
        base = rank_candidate(row, spec)
        dur = float(row.get("duration") or 0)
        title = str(row.get("title") or "")
        if dur >= pref:
            base += 3.0
        elif dur > 0 and dur < pref * 0.6:
            base -= 4.0
        if COMBAT_TITLE_RE.search(title):
            base += 2.5
        return base

    return sorted(candidates, key=lambda row: -_score(row))


def pick_discovery_candidate(game: str, candidates: list[dict]) -> dict | None:
    ranked = rank_discovery_candidates(game, candidates)
    if not ranked:
        return None
    g = game.strip().lower()
    pref = _pref_min_sec()
    if g == "pubg":
        from pubg_metro_royale_gate import title_metro_hint
        from youtube_game_prefs import russian_score

        long_enough = [c for c in ranked if float(c.get("duration") or 0) >= pref]
        combat_long = [
            c for c in long_enough if COMBAT_TITLE_RE.search(str(c.get("title") or ""))
        ]
        pool = combat_long or long_enough or ranked
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
    if g == "standoff":
        from youtube_game_prefs import russian_score

        long_enough = [c for c in ranked if float(c.get("duration") or 0) >= pref]
        combat_long = [
            c for c in long_enough if COMBAT_TITLE_RE.search(str(c.get("title") or ""))
        ]
        pool = combat_long or long_enough or ranked
        for cand in pool[:12]:
            if russian_score(cand) >= 0.06 and COMBAT_TITLE_RE.search(str(cand.get("title") or "")):
                return cand
        for cand in pool[:12]:
            if COMBAT_TITLE_RE.search(str(cand.get("title") or "")):
                return cand
        return pool[0]
    return ranked[0]


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
