#!/usr/bin/env python3
"""Cheap pre-download MLBB hero/role filtering for kill-heavy VODs."""

from __future__ import annotations

import json
import os
import re
from functools import lru_cache
from pathlib import Path


SUPPORT_OR_ROAM_HEROES = frozenset(
    {
        "angela",
        "atlas",
        "belerick",
        "carmilla",
        "chip",
        "diggie",
        "estes",
        "floryn",
        "franco",
        "grock",
        "hylos",
        "johnson",
        "kaja",
        "khufra",
        "lolita",
        "mathilda",
        "minotaur",
        "rafaela",
        "tigreal",
    }
)

COMBAT_SIGNAL_RE = re.compile(
    r"(?:savage|maniac|triple\s+kill|double\s+kill|ruthless|"
    r"\b\d{1,2}\s*(?:kills?|убийств)|team\s*wipe|wipe(?:s|d)?\s+(?:the\s+)?enemy|"
    r"one\s+shot|unstoppable|hyper\s*carry|mvp|clutch|"
    r"саваж|маньяк|тройн\w*\s+убийств|двойн\w*\s+убийств)",
    re.I,
)
ROAM_CONTEXT_RE = re.compile(r"\b(?:roam(?:er)?|support|tank)\b", re.I)


@lru_cache(maxsize=1)
def hero_tags() -> dict[str, tuple[str, ...]]:
    repo = Path(os.environ.get("CONTENT_BOT_REPO", "/root/content_bot_ml"))
    path = repo / "config" / "mlbb_heroes.json"
    result: dict[str, tuple[str, ...]] = {
        hero: (hero,) for hero in SUPPORT_OR_ROAM_HEROES
    }
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return result
    for row in data.get("heroes", []):
        hero = str(row.get("id") or "").strip().lower()
        if not hero:
            continue
        tags = tuple(
            dict.fromkeys(
                [hero.replace("_", " "), *[str(tag).strip().lower() for tag in row.get("tags", [])]]
            )
        )
        result[hero] = tags
    return result


def heroes_from_title(title: str) -> list[str]:
    blob = f" {str(title or '').lower()} "
    found: list[str] = []
    for hero, tags in hero_tags().items():
        for tag in tags:
            if tag and re.search(rf"(?<![a-z0-9]){re.escape(tag)}(?![a-z0-9])", blob):
                found.append(hero)
                break
    return found


def support_heroes_from_title(title: str) -> list[str]:
    return [hero for hero in heroes_from_title(title) if hero in SUPPORT_OR_ROAM_HEROES]


def passes_vod_hero_gate(title: str) -> tuple[bool, str]:
    """Reject support/roam-primary VODs unless the title promises real multi-kill action."""
    if os.environ.get("MLBB_VOD_HERO_GATE", "1") != "1":
        return True, "hero_gate_disabled"
    supports = support_heroes_from_title(title)
    roam_context = bool(ROAM_CONTEXT_RE.search(str(title or "")))
    if not supports and not roam_context:
        return True, "carry_or_unknown"
    if COMBAT_SIGNAL_RE.search(str(title or "")):
        return True, "support_with_combat_signal"
    detail = ",".join(supports) if supports else "roam"
    return False, f"support_without_multikill:{detail}"


def vod_hero_rank_adjustment(title: str) -> float:
    supports = support_heroes_from_title(title)
    if not supports and not ROAM_CONTEXT_RE.search(str(title or "")):
        return 0.0
    return 2.0 if COMBAT_SIGNAL_RE.search(str(title or "")) else -14.0
