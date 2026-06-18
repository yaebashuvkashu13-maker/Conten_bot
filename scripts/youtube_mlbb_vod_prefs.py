#!/usr/bin/env python3
"""MLBB ranked VOD discovery: title/channel filters and candidate ranking."""

from __future__ import annotations

import re

# Hard reject — montages, guides, promos; unlikely to yield fight segments.
BAD_TITLE_RE = re.compile(
    r"(?:"
    r"giveaway|#short\b|shorts\b|tiktok\b|reels?\b|"
    r"montage|compilation|compilación|highlight(?:s)?\s+reel|best\s+(?:moment|play)|"
    r"top\s+\d+\s+(?:play|moment|savage)|savage\s+montage|"
    r"tutorial|beginner\s+guide|how\s+to\s+(?:play|use|build)|tips\s+and\s+tricks|"
    r"build\s+guide|item\s+build|emblem\s+guide|"
    r"reaction(?:\s+only)?|react(?:ing|s)?\s+to|"
    r"official\s+cinematic|trailer\b|cinematic\b|"
    r"skin\s+review|new\s+skin|skin\s+showcase|diamond\s+giveaway|"
    r"patch\s+notes|update\s+review|new\s+hero\s+release|"
    r"funny\s+moments?|troll(?:ing)?|meme\s+comp|"
    r"music\s+video|edited\s+by|fan\s*made|"
    r"news\b|esports\s+recap|mpl\s+highlights|tournament\s+highlights"
    r")",
    re.I,
)

# Extra reject when title lacks ranked/match signals — face-cam / variety streams.
SOFT_BAD_TITLE_RE = re.compile(
    r"(?:"
    r"just\s+chatting|q\s*&\s*a|opening\s+diamonds?|diamond\s+spin|"
    r"gacha|lucky\s+spin|account\s+review|coach(?:ing)?\s+session|"
    r"rank\s+push\s+stream(?!\s+gameplay)"
    r")",
    re.I,
)

RANKED_SIGNAL_RE = re.compile(
    r"\b(?:ranked?|mythic|legend|epic|grandmaster|immortal|solo\s*queue?|"
    r"match|gameplay|full\s+(?:game|match)|replay|vs\.?)\b",
    re.I,
)

DEFAULT_SEARCH_QUERIES = (
    "MLBB mythic ranked full match gameplay 20 minutes,"
    "Mobile Legends legend rank solo queue full match replay,"
    "MLBB ranked match gameplay no montage 15 minutes,"
    "Mobile Legends mythic ranked solo match full game"
)


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


def rank_mlbb_vod_candidate(meta: dict, *, target_dur_sec: float = 1500.0) -> float:
    """Higher score = better candidate for ranked fight extraction."""
    blob = text_blob(meta).lower()
    dur = float(meta.get("duration") or 0)
    score = 0.0

    # Prefer ~20–25 min matches (sweet spot for teamfights without 45 min scan).
    score -= abs(dur - target_dur_sec) / 240.0

    boosts = (
        ("full match", 5.0),
        ("full game", 5.0),
        ("ranked", 4.0),
        ("mythic", 4.0),
        ("legend", 3.0),
        ("immortal", 3.0),
        ("grandmaster", 2.5),
        ("solo queue", 3.0),
        ("solo rank", 3.0),
        ("gameplay", 2.0),
        ("replay", 2.5),
        ("match", 2.0),
        (" vs ", 2.5),
        ("savage", 1.5),
        ("teamfight", 1.5),
        ("no commentary", 1.0),
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
        ("skin", -6.0),
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
    )
    for needle, weight in penalties:
        if needle in blob:
            score += weight

    return score


def normalize_uploader(meta: dict) -> str:
    return str(meta.get("uploader") or meta.get("channel") or "").strip().casefold()
