#!/usr/bin/env python3
"""Normalize owner dislike reasons / free-text notes for PUBG ranker training.

Reasons are NOT used as inference features (unknown on new peaks). They adjust
sample weights and are stored in the dataset for analysis / future models.
"""

from __future__ import annotations

import re

# code -> weight multiplier (applied on top of base sample weight)
REASON_WEIGHTS: dict[str, float] = {
    "loot_run": 1.85,
    "menu_lobby": 1.75,
    "no_combat": 1.80,
    "no_kill": 1.25,
    "boring": 1.35,
    "promo": 1.55,
    "not_metro": 1.70,
    "classic": 1.60,
    "author_death": 1.20,
    "blurry": 1.15,
    "other": 1.10,
    "fight": 1.55,  # explicit good fight / window_tg good
    "style_ref": 1.40,
    "trash": 1.50,
}

_ALIASES: dict[str, str] = {
    "loot": "loot_run",
    "loot_run": "loot_run",
    "menu": "menu_lobby",
    "menu_lobby": "menu_lobby",
    "lobby": "menu_lobby",
    "no_combat": "no_combat",
    "no_kill": "no_kill",
    "boring": "boring",
    "promo": "promo",
    "not_metro": "not_metro",
    "not_metro_royale": "not_metro",
    "classic": "classic",
    "author_death": "author_death",
    "blurry": "blurry",
    "other": "other",
    "fight": "fight",
    "owner_trash_part": "trash",
    "owner_reject_trash_2026-08-05": "trash",
}


def normalize_reason(*parts: str) -> str:
    """Map reason code or free-text note → stable reason code (may be '')."""
    raw = " ".join(str(p or "") for p in parts).strip().lower()
    if not raw:
        return ""
    # Exact / alias token
    compact = raw.replace(" ", "_")
    if compact in _ALIASES:
        return _ALIASES[compact]
    if raw in _ALIASES:
        return _ALIASES[raw]

    # Keyword rules for owner free-text comments
    rules: list[tuple[str, str]] = [
        (r"loot|лут|бег без боя|run no starter", "loot_run"),
        (r"menu|lobby|меню|лобби", "menu_lobby"),
        (r"no[_ ]?combat|нет\s*перестрел|нет\s*боя|silent", "no_combat"),
        (r"no[_ ]?kill|килл не|нет\s*килл", "no_kill"),
        (r"boring|скучн", "boring"),
        (r"promo|реклам", "promo"),
        (r"not[_ ]?metro|не\s*metro|классиik|classic|небо", "not_metro"),
        (r"author[_ ]?death|смерть\s*автора", "author_death"),
        (r"blurry|мыльн", "blurry"),
        (r"trash|reject_trash", "trash"),
        (r"fight\s*style|target fight|normal fight|fight at end|part \d+ reference", "style_ref"),
        (r"\bfight\b|файт|перестрел", "fight"),
    ]
    for pattern, code in rules:
        if re.search(pattern, raw, flags=re.IGNORECASE):
            return code
    return "other" if raw else ""


def weight_multiplier(reason: str, *, label: int) -> float:
    """Stronger weight for clear hard negatives / clear fight positives."""
    code = normalize_reason(reason)
    if not code:
        return 1.0
    mult = float(REASON_WEIGHTS.get(code, 1.1))
    if label == 1 and code in {"loot_run", "menu_lobby", "no_combat", "trash", "promo", "not_metro"}:
        # Conflicting: marked good but note says junk — trust note less, mild downweight
        return 0.85
    if label == 0 and code == "fight":
        return 0.90
    if label == 1 and code in {"fight", "style_ref"}:
        return mult
    if label == 0:
        return mult
    return 1.05
