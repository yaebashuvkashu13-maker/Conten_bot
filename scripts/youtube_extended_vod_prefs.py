#!/usr/bin/env python3
"""YouTube discovery for Genshin / WoT VOD segments."""

from __future__ import annotations

import os
import re
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote_plus

import yaml

from youtube_mlbb_vod_prefs import (
    YOUTUBE_DURATION_SP_4_TO_20,
    YOUTUBE_FRESHNESS_SP_THIS_MONTH,
    upload_age_days,
)
from youtube_shooter_vod_prefs import BAD_TITLE_RE, LIVE_TITLE_RE

GENSHIN_TITLE_RE = re.compile(r"genshin|геншин|原神", re.I)
WOT_TITLE_RE = re.compile(
    r"world\s+of\s+tanks|wot\s*blitz|tanks?\s*blitz|танк|blitz|wotb",
    re.I,
)
GENSHIN_BAD_TITLE_RE = re.compile(
    r"banner|wish|gacha|обзор\s*персонаж|build\s*guide|tier\s*list|story\s*quest",
    re.I,
)
WOT_BAD_TITLE_RE = re.compile(
    r"premium\s*shop|giveaway|обзор\s*танка|grind\s*guide|tier\s*list|trailer",
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
    "WoT Blitz rating battle gameplay no commentary",
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
    "World of Tanks Blitz 7 kills ace replay",
    "WoT Blitz supremacy mode full match",
)


def _repo_root() -> Path:
    env = os.environ.get("CONTENT_BOT_REPO", "").strip()
    if env:
        return Path(env)
    root = Path(__file__).resolve().parent.parent
    if root.name == "bin" or str(root) == "/usr/local":
        return Path("/root/content_bot_ml")
    return root


@lru_cache(maxsize=1)
def _wot_query_bank() -> dict:
    path = Path(os.environ.get("WOT_YOUTUBE_QUERY_BANK", str(_repo_root() / "data/wot/youtube_query_bank.yaml")))
    if not path.exists():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return data if isinstance(data, dict) else {}
    except (OSError, yaml.YAMLError):
        return {}


def _wot_reject_patterns() -> list[re.Pattern[str]]:
    bank = _wot_query_bank()
    raw = bank.get("reject_title_patterns") or []
    patterns = [WOT_BAD_TITLE_RE, LIVE_TITLE_RE, BAD_TITLE_RE]
    for item in raw:
        try:
            patterns.append(re.compile(str(item), re.I))
        except re.error:
            continue
    extra = os.environ.get("WOT_DISCOVERY_REJECT_TITLE_RE", "").strip()
    if extra:
        try:
            patterns.append(re.compile(extra, re.I))
        except re.error:
            pass
    return patterns


def _queries_for(game: str) -> tuple[str, ...]:
    g = game.strip().lower()
    if g == "wot":
        bank = _wot_query_bank()
        if bank:
            parts: list[str] = []
            for key in ("core_queries", "combat_queries", "angle_queries", "ru_queries"):
                chunk = bank.get(key) or []
                if isinstance(chunk, list):
                    parts.extend(str(x) for x in chunk if str(x).strip())
            if parts:
                return tuple(dict.fromkeys(parts))
        return WOT_CORE_QUERIES + WOT_ANGLE_QUERIES
    return GENSHIN_CORE_QUERIES + GENSHIN_ANGLE_QUERIES


def wot_target_duration_sec() -> float:
    bank = _wot_query_bank()
    meta = bank.get("meta") if isinstance(bank.get("meta"), dict) else {}
    return float(
        os.environ.get(
            "WOT_VOD_TARGET_DUR_SEC",
            meta.get("target_duration_sec", 390),
        )
    )


def wot_recommended_duration_window() -> tuple[float, float]:
    bank = _wot_query_bank()
    meta = bank.get("meta") if isinstance(bank.get("meta"), dict) else {}
    min_sec = float(os.environ.get("WOT_VOD_MIN_SEC", meta.get("recommended_min_sec", 120)))
    max_sec = float(os.environ.get("WOT_VOD_MAX_SEC", meta.get("recommended_max_sec", 1500)))
    return min_sec, max_sec


def wot_youtube_duration_sp(env: dict[str, str] | None = None) -> str:
    env = env or {}
    explicit = (env.get("WOT_VOD_YOUTUBE_DURATION_SP") or "").strip()
    if explicit:
        return explicit
    bank = _wot_query_bank()
    meta = bank.get("meta") if isinstance(bank.get("meta"), dict) else {}
    if env.get("WOT_VOD_YOUTUBE_DURATION_FILTER", "1") == "0":
        return ""
    return str(meta.get("youtube_duration_sp") or YOUTUBE_DURATION_SP_4_TO_20)


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
        if not WOT_TITLE_RE.search(t):
            return False
        for pat in _wot_reject_patterns():
            if pat.search(t):
                return False
        return True
    return False


def rank_wot_vod_candidate(meta: dict) -> float:
    """Higher = better WoT VOD for combat segment mining."""
    title = str(meta.get("title") or "")
    blob = " ".join(
        [
            title,
            str(meta.get("uploader") or meta.get("channel") or ""),
            " ".join(str(x) for x in (meta.get("tags") or [])[:10]),
        ]
    ).lower()
    if not title_ok("wot", title):
        return -999.0

    dur = float(meta.get("duration") or 0)
    target = wot_target_duration_sec()
    min_sec, max_sec = wot_recommended_duration_window()
    score = 0.0

    if dur > 0:
        if dur < min_sec or dur > max_sec:
            score -= 25.0
        score -= abs(dur - target) / 90.0
        if 240 <= dur <= 720:
            score += 8.0
        elif 120 <= dur < 240:
            score += 4.0
        elif 720 < dur <= 1200:
            score += 2.0
        elif dur > 1800:
            score -= 18.0

    age = upload_age_days(str(meta.get("upload_date") or ""))
    if age is not None:
        if age <= 7:
            score += 5.0
        elif age <= 21:
            score += 2.0
        elif age > 45:
            score -= 8.0

    bank = _wot_query_bank()
    boosts = bank.get("boost_keywords") or []
    if isinstance(boosts, list):
        for item in boosts:
            if isinstance(item, (list, tuple)) and len(item) == 2:
                needle, weight = str(item[0]).lower(), float(item[1])
                if needle in blob:
                    score += weight

    penalties = (
        ("live", -20.0),
        ("stream", -18.0),
        ("стрим", -18.0),
        ("tournament", -14.0),
        ("grand final", -16.0),
        ("cup", -8.0),
        ("chill", -10.0),
        ("chatting", -10.0),
        ("guide", -12.0),
        ("гайд", -12.0),
        ("how to", -12.0),
        ("montage", -15.0),
        ("compilation", -15.0),
        ("highlight", -10.0),
        ("хайлайт", -10.0),
        ("shorts", -20.0),
        ("trailer", -20.0),
        ("review", -6.0),
        ("обзор", -8.0),
    )
    for needle, weight in penalties:
        if needle in blob:
            score += weight

    if os.environ.get("WOT_DISCOVERY_PREFER_RUSSIAN", "1") == "1":
        if re.search(r"[а-яё]", blob, re.I):
            score += 2.5
    return round(score, 3)


def rank_extended_vod_candidates(game: str, candidates: list[dict]) -> list[dict]:
    g = game.strip().lower()
    if g != "wot" or not candidates:
        return candidates
    return sorted(candidates, key=lambda row: -rank_wot_vod_candidate(row))


def pick_discovery_candidate(game: str, candidates: list[dict]) -> dict | None:
    ranked = rank_extended_vod_candidates(game, candidates)
    for row in ranked:
        if title_ok(game, str(row.get("title") or "")):
            return row
    return None


def vod_discovery_search_cycle(cycle: int, game: str, env: dict[str, str] | None = None) -> dict[str, object]:
    env = env or {}
    queries = list(_queries_for(game))
    batch = int(env.get("EXTENDED_VOD_SEARCH_BATCH", env.get("SHOOTER_VOD_SEARCH_BATCH", "3")))
    delay = float(env.get("EXTENDED_VOD_SEARCH_DELAY", env.get("SHOOTER_VOD_SEARCH_DELAY", "6")))
    limit = int(env.get("EXTENDED_VOD_SEARCH_LIMIT", env.get("SHOOTER_VOD_SEARCH_LIMIT", "20")))
    if not queries:
        return {"queries": [], "batch": batch, "delay": delay, "limit": limit, "sp": ""}
    offset = (cycle * batch) % len(queries)
    picked = [queries[(offset + i) % len(queries)] for i in range(batch)]
    sp = env.get("MLBB_VOD_YOUTUBE_DURATION_FILTER", "1") == "1"
    freshness = (
        YOUTUBE_FRESHNESS_SP_THIS_MONTH
        if env.get("MLBB_VOD_SEARCH_FRESH", "1") == "1"
        else ""
    )
    if game.strip().lower() == "wot":
        duration_sp = wot_youtube_duration_sp(env) if sp else ""
    else:
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
        "duration_sp": duration_sp,
    }
