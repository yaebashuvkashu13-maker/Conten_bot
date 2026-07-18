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
    r"pubg|playerunknown|battlegrounds|metro\s*royale?|пабг|метро\s*роял[еь]?",
    re.I,
)
METRO_VAGUE_TITLE_RE = re.compile(
    r"gone without|without a trace|tips\s+and\s+tricks|highlights?|"
    r"обзор|гайд|guide|story|история|сюжет|"
    r"how\s+to\s+get|how\s+to\s+farm|how\s+to\s+spawn|free\s+karambit|karambit|керамбит|"
    r"mysterious\s+voucher|gold\s+tickets?|fabled|"
    r"loot\s+run|loot\s+farm|million.?loot|new\s+metro\s+royale?\s+map|"
    r"zero\s+to\s+hero|с\s+нуля\s+до|get\s+rich|strategy\s+to\s+get|"
    r"prize\s*path|honor\s+system|mission\s+not|rankings?\s+in|"
    r"обновлен|patch\s+notes|creator\s+club",
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
    r"reaction|trailer|cinematic|aim\s*trainer|training\s*mode|\btraining\b|"
    r"highlight|highlights|хайлайт|tips\s+and\s+tricks|tips\s*&\s*tricks|\btips?\b|"
    r"guide|обзор|trick|совет|"
    r"how\s+to\s+get|how\s+to\s+farm|how\s+to\s+spawn|free\s+karambit|karambit|керамбит|"
    r"mysterious\s+voucher|voucher|mystery\s+ticket|gold\s+tickets?|fabled\s+mk|"
    r"loot\s+run|loot\s+farm|открытие|крафт|case\s*opening|billion\s+for\s+case|"
    r"sensitivity|no[\s-]?recoil|zero[\s-]?recoil|трениров|чувствительн|"
    r"zero\s+to\s+hero|с\s+нуля\s+до|"
    r"🔴|\bLIVE\b|boss\s*drop|knife\s+drops?|что\s+падает|сопровожден|"
    r"учусь\s+играть|learning\s+to\s+play|beginner|новичок|"
    r"первый\s+раз|first\s+time\s+play|noob\s+learn|"
    r"prize\s*path|honor\s+system|mission\s+not|rankings?\s+in|"
    r"обновлен|patch\s+notes|creator\s+club",
    re.I,
)
COMBAT_TITLE_RE = re.compile(
    r"fight|clutch|squad\s*wipe|перестрел|файт|бой|ranked|ранкед|"
    r"sniper|снайпер|1v\d|\bv[s]?\d+\b|\bvs\b|solo\s+vs|one\s+vs|"
    r"один\s+против|против\s+пач|против\s+сквад|соло|"
    r"эвакуац|extract|катки|рейд|raid|wipe|дуэл|duel|rush|пуш|"
    r"escape|flame|trap|corner|handcam|вебк|пачк|"
    r"battle|showdown|assault|штурм|кил|убил|вынес|против|сквад|squad|"
    r"deadmatch|deathmatch|close[\s-]?range|close[\s-]?fight|duo\s+vs",
    re.I,
)
MATCH_TITLE_RE = re.compile(
    r"match|матч|gameplay|геймплей|full\s+game|полный|replay|повтор|"
    r"катка|нарезк",
    re.I,
)

PUBG_CORE_QUERIES = (
    "PUBG Mobile Metro Royale clutch fight gameplay",
    "PUBG Mobile Metro Royale 1v1 fight ranked",
    "PUBG Mobile Metro Royale squad wipe fight",
    "PUBG Mobile Metro Royale duo vs squads",
    "метро рояль пабг файт перестрелка",
    "метро рояль пабг мобайл ранкед матч",
    "метро рояль пабг клип файт",
    "метро рояль дуо против сквада",
    "PUBG Mobile Metro Royale extract fight",
    "пабг метро рояль катки файт",
    "метро рояль пабг снайпер дуэль",
    "PUBG Mobile Metro Royale close range fight",
    "пабг метро рояль соло против пачек",
)

STANDOFF_CORE_QUERIES = (
    "Standoff 2 ranked gameplay full match",
    "Standoff 2 competitive gameplay replay",
    "Standoff 2 clutch ranked match",
    "Standoff 2 5v5 ranked gameplay",
)

PUBG_ANGLE_QUERIES = (
    "PUBG Mobile Metro Royale sniper fight",
    "метро рояль пабг 1v4 clutch",
    "метро рояль пабг соло против сквада",
    "PUBG Metro Royale rush fight gameplay",
    "метро рояль пабг эвакуация файт",
)

STANDOFF_ANGLE_QUERIES = (
    "Standoff 2 ace ranked gameplay",
    "Standoff 2 teamfight ranked replay",
)


def _queries_for(game: str, env: dict[str, str] | None = None) -> tuple[str, ...]:
    g = game.strip().lower()
    env = env or os.environ
    if g == "standoff":
        raw = str(env.get("STANDOFF_VOD_SEARCH_QUERIES") or "").strip()
        if raw:
            custom = tuple(q.strip() for q in raw.split(",") if q.strip())
            if custom:
                return custom
        return STANDOFF_CORE_QUERIES + STANDOFF_ANGLE_QUERIES
    # Prefer operator-tuned PUBG query CSV from env when present.
    raw = str(env.get("PUBG_VOD_SEARCH_QUERIES") or env.get("SHOOTER_PUBG_SEARCH_QUERIES") or "").strip()
    if raw:
        custom = tuple(q.strip() for q in raw.split(",") if q.strip())
        if custom:
            return custom
    return PUBG_CORE_QUERIES + PUBG_ANGLE_QUERIES


def title_ok(game: str, title: str, *, uploader: str = "") -> bool:
    t = title or ""
    g = game.strip().lower()
    if LIVE_TITLE_RE.search(t) or BAD_TITLE_RE.search(t):
        return False
    if g == "standoff":
        return bool(STANDOFF_TITLE_RE.search(t))
    if g == "pubg":
        if CLASSIC_MODE_TITLE_RE.search(t) or METRO_VAGUE_TITLE_RE.search(t):
            return False
        meta = {"title": t, "uploader": uploader or ""}
        if not (has_metro_royale(meta) and bool(PUBG_TITLE_RE.search(t))):
            return False
        # Drop mission/guide noise that still mentions Metro — need fight or match signal.
        return bool(COMBAT_TITLE_RE.search(t) or MATCH_TITLE_RE.search(t))
    return False


def rank_discovery_candidates(game: str, candidates: list[dict]) -> list[dict]:
    """Prefer Metro + Russian + combat-ish titles for PUBG discovery."""
    if not candidates:
        return candidates
    g = game.strip().lower()
    if g != "pubg" or os.environ.get("SHOOTER_VOD_PREFER_RUSSIAN", "1") != "1":
        return candidates
    from youtube_game_prefs import rank_candidate

    spec = {"require_metro_royale": True, "prefer_russian": True}

    def _score(row: dict) -> float:
        base = float(rank_candidate(row, spec))
        title = str(row.get("title") or "")
        if COMBAT_TITLE_RE.search(title):
            base += 2.5
        dur = float(row.get("duration") or 0)
        if 300 <= dur <= 1800:
            base += 1.0
        if 480 <= dur <= 1200:
            base += 0.5
        return base

    return sorted(candidates, key=_score, reverse=True)


def pick_discovery_candidate(game: str, candidates: list[dict]) -> dict | None:
    ranked = rank_discovery_candidates(game, candidates)
    if not ranked:
        return None
    if game.strip().lower() == "pubg":
        from pubg_metro_royale_gate import title_metro_hint
        from youtube_game_prefs import russian_score

        for cand in ranked[:12]:
            title = str(cand.get("title") or "")
            if title_metro_hint(title) and russian_score(cand) >= 0.06:
                return cand
        for cand in ranked[:12]:
            if title_metro_hint(str(cand.get("title") or "")):
                return cand
        for cand in ranked[:8]:
            if russian_score(cand) >= 0.12:
                return cand
    return ranked[0]


def vod_discovery_search_cycle(cycle: int, game: str, env: dict[str, str] | None = None) -> dict[str, object]:
    """Rotate shooter search queries + YouTube filters (freshness / duration / week).

    YouTube ``sp`` encodes one filter set — duration and freshness cannot be OR-combined
    in a single URL, so we rotate modes across cycles (same idea as MLBB).
    """
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
