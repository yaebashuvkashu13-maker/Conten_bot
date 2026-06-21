#!/usr/bin/env python3
"""MLBB ranked VOD discovery: title/channel filters and candidate ranking."""

from __future__ import annotations

import os
import re
from urllib.parse import quote_plus

# YouTube search UI: "4–20 minutes" duration bucket (closest to our 3–20 min window).
YOUTUBE_DURATION_SP_4_TO_20 = "EgQQARgB"

MLBB_VOD_DEFAULT_SEASON = 41

# Popular heroes for rotating VOD search (includes Masha from user examples).
VOD_SEARCH_HEROES = (
    "masha",
    "paquito",
    "hayabusa",
    "gusion",
    "fanny",
    "ling",
    "chou",
    "beatrix",
    "moskov",
    "valentina",
    "joy",
    "angela",
    "tigreal",
    "layla",
    "kagura",
    "lancelot",
)

# Hard reject — montages, guides, promos, skin showcases.
BAD_TITLE_RE = re.compile(
    r"(?:"
    r"giveaway|#short\b|shorts\b|tiktok\b|reels?\b|"
    r"montage|compilation|compilación|highlight(?:s)?\s+reel|best\s+(?:moment|play)|"
    r"top\s+\d+\s+(?:play|moment|savage)|savage\s+montage|"
    r"tutorial|beginner\s+guide|how\s+to\s+(?:play|use|build)|tips\s+and\s+tricks|"
    r"build\s+guide|item\s+build|emblem\s+guide|"
    r"reaction(?:\s+only)?|react(?:ing|s)?\s+to|"
    r"official\s+cinematic|trailer\b|cinematic\b|"
    r"skin\s+review|new\s+skin|skin\s+showcase|skin\s+comparison|all\s+skins?\b|"
    r"collector\s+skin|starlight\s+skin|legendary\s+skin|epic\s+skin|"
    r"skin\s+(?:unbox|preview|trailer|animation|effect|test)|"
    r"battle\s+pass\s+skin|event\s+skin|limited\s+skin|exorcist\s+skin|"
    r"new\s+(?:collector|legendary|epic|starlight|limited)\b|"
    r"season\s+\d+\s+skin|skin\s+season|diamond\s+giveaway|"
    r"patch\s+notes|update\s+review|new\s+hero\s+release|"
    r"funny\s+moments?|troll(?:ing)?|meme\s+comp|"
    r"music\s+video|edited\s+by|fan\s*made|"
    r"news\b|esports\s+recap|mpl\s+highlights|tournament\s+highlights|"
    r"обзор.{0,24}скин|скин.{0,24}обзор|новый\s+скин|показ\s+скина"
    r")",
    re.I,
)

# Extra reject when title lacks ranked/match signals — face-cam / variety streams.
SOFT_BAD_TITLE_RE = re.compile(
    r"(?:"
    r"just\s+chatting|q\s*&\s*a|opening\s+diamonds?|diamond\s+spin|"
    r"gacha|lucky\s+spin|account\s+review|coach(?:ing)?\s+session|"
    r"rank\s+push\s+stream(?!\s+gameplay)|skin\s+spin|lucky\s+box"
    r")",
    re.I,
)

RANKED_SIGNAL_RE = re.compile(
    r"\b(?:ranked?|mythic|legend|epic|grandmaster|immortal|solo\s*queue?|"
    r"match|gameplay|full\s+(?:game|match)|replay|vs\.?|global)\b",
    re.I,
)


def vod_current_season() -> int:
    raw = (os.environ.get("MLBB_VOD_SEASON") or "").strip()
    if raw.isdigit():
        return int(raw)
    return MLBB_VOD_DEFAULT_SEASON


def build_vod_search_queries(
    *,
    season: int | None = None,
    heroes: tuple[str, ...] | None = None,
    max_hero_queries: int = 8,
) -> list[str]:
    """Search phrases without duration — YouTube duration filter is applied separately."""
    season = season if season is not None else vod_current_season()
    heroes = heroes or VOD_SEARCH_HEROES
    queries = [
        f"MLBB mythic global ranked gameplay season {season}",
        f"Mobile Legends mythic global solo queue season {season}",
        f"MLBB legend rank global full match season {season}",
    ]
    for hero in heroes[:max_hero_queries]:
        queries.append(f"MLBB mythic global {hero} season {season} ranked gameplay")
    return queries


def default_vod_search_queries_csv() -> str:
    return ",".join(build_vod_search_queries())


DEFAULT_SEARCH_QUERIES = default_vod_search_queries_csv()


def youtube_results_search_url(query: str, *, duration_sp: str = "") -> str:
    """YouTube results URL; optional sp= applies the site duration filter (not query text)."""
    url = f"https://www.youtube.com/results?search_query={quote_plus(query)}"
    sp = (duration_sp or "").strip()
    if sp:
        url += f"&sp={sp}"
    return url


def vod_youtube_duration_sp(env: dict[str, str] | None = None) -> str:
    merged = {**os.environ, **(env or {})}
    explicit = (merged.get("MLBB_VOD_YOUTUBE_DURATION_SP") or "").strip()
    if explicit.lower() in ("0", "off", "none", "disable", "disabled"):
        return ""
    if explicit:
        return explicit
    if merged.get("MLBB_VOD_YOUTUBE_DURATION_FILTER", "1") == "0":
        return ""
    return YOUTUBE_DURATION_SP_4_TO_20


def text_blob(meta: dict) -> str:
    parts = [
        str(meta.get("title") or ""),
        str(meta.get("uploader") or meta.get("channel") or ""),
        " ".join(str(t) for t in (meta.get("tags") or [])[:12]),
    ]
    return " ".join(parts)


def passes_mlbb_vod_filters(meta: dict) -> bool:
    blob = text_blob(meta)
    if BAD_TITLE_RE.search(blob):
        return False
    if SOFT_BAD_TITLE_RE.search(blob) and not RANKED_SIGNAL_RE.search(blob):
        return False
    return True


def rank_mlbb_vod_candidate(meta: dict, *, target_dur_sec: float = 780.0) -> float:
    """Higher score = better candidate for ranked fight extraction."""
    blob = text_blob(meta).lower()
    dur = float(meta.get("duration") or 0)
    score = 0.0

    # Prefer ~10–15 min uploads (typical ranked match length in the 3–20 min window).
    score -= abs(dur - target_dur_sec) / 180.0

    boosts = (
        ("full match", 5.0),
        ("full game", 5.0),
        ("ranked", 4.0),
        ("mythic", 4.0),
        ("legend", 3.0),
        ("immortal", 3.0),
        ("grandmaster", 2.5),
        ("global", 3.5),
        ("solo queue", 3.0),
        ("solo rank", 3.0),
        ("gameplay", 2.0),
        ("replay", 2.5),
        ("match", 2.0),
        (" vs ", 2.5),
        ("savage", 1.5),
        ("teamfight", 1.5),
        ("no commentary", 1.0),
        (f"season {vod_current_season()}", 2.5),
    )
    for needle, weight in boosts:
        if needle in blob:
            score += weight

    penalties = (
        ("montage", -12.0),
        ("compilation", -12.0),
        ("highlight", -8.0),
        ("best moment", -8.0),
        ("tutorial", -10.0),
        ("guide", -6.0),
        ("reaction", -8.0),
        ("skin", -10.0),
        ("collector", -8.0),
        ("starlight", -8.0),
        ("legendary skin", -10.0),
        ("giveaway", -12.0),
        ("funny", -4.0),
        ("edit", -3.0),
        ("music", -5.0),
        ("shorts", -12.0),
        ("tiktok", -12.0),
        ("cinematic", -8.0),
        ("trailer", -10.0),
        ("update", -4.0),
        ("patch", -4.0),
        ("live stream", -6.0),
        ("uncut", -5.0),
        ("streamer", -1.5),
        ("face cam", -4.0),
        ("unbox", -8.0),
        ("preview", -5.0),
    )
    for needle, weight in penalties:
        if needle in blob:
            score += weight

    return score


def normalize_uploader(meta: dict) -> str:
    return str(meta.get("uploader") or meta.get("channel") or "").strip().casefold()
