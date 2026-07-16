#!/usr/bin/env python3
"""Per-game YouTube discovery preferences (Metro Royale, RU streamers, etc.)."""

from __future__ import annotations

import re

METRO_ROYALE_RE = re.compile(
    r"metro[\s_-]*royale|metroroyale|метро[\s_-]*роял|метророял",
    re.I,
)
CYRILLIC_RE = re.compile(r"[а-яё]", re.I)
GENERIC_PUBG_ONLY_RE = re.compile(
    r"^(?!.*(metro|metroroyale|метро)).*\b(pubg|пабг)\b",
    re.I,
)


def text_blob(meta: dict) -> str:
    return f"{meta.get('title', '')} {meta.get('uploader', '')}"


def has_metro_royale(meta: dict) -> bool:
    return bool(METRO_ROYALE_RE.search(text_blob(meta)))


def russian_score(meta: dict) -> float:
    """Higher = more Cyrillic in title/channel (RU streamer hint)."""
    blob = text_blob(meta)
    letters = [c for c in blob if c.isalpha()]
    if not letters:
        return 0.0
    cyr = sum(1 for c in letters if CYRILLIC_RE.match(c))
    return cyr / len(letters)


def rank_candidate(meta: dict, game: dict) -> float:
    """Sort key for pick_candidate (higher = better)."""
    score = 0.0
    dur = float(meta.get("duration") or 0)
    # VOD discovery window is typically 4–20 min — don't prefer 2h streams.
    ideal = float(game.get("ideal_duration_sec") or 900.0)
    scale = max(300.0, ideal / 3.0)
    score -= abs(dur - ideal) / scale

    if game.get("require_metro_royale"):
        if has_metro_royale(meta):
            score += 8.0
        else:
            score -= 12.0

    if game.get("prefer_russian"):
        score += min(4.0, russian_score(meta) * 6.0)
        if russian_score(meta) >= 0.15:
            score += 1.5

    return score


def passes_game_filters(meta: dict, game: dict) -> bool:
    if game.get("require_metro_royale") and not has_metro_royale(meta):
        return False
    if game.get("prefer_russian") and russian_score(meta) < 0.08:
        # Soft filter only when we have other candidates; hard-skip in discover if strict
        if game.get("require_russian"):
            return False
    return True
