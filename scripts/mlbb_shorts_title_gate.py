#!/usr/bin/env python3
"""Title-only MLBB Shorts filters — no OpenCV dependency."""

from __future__ import annotations

import re

PROMO_PATTERNS = re.compile(
    r"(#ad\b|sponsored|giveaway|promo\b|free\s+diamond|skin\s+gratis|"
    r"log\s*in\s+mlbb|mailbox|click\s+link|download\s+now|official\s+event)",
    re.I,
)

OTHER_GAME_TITLE = re.compile(
    r"(pubg|bgmi|standoff\s*2|standoff2|genshin|honkai|free\s*fire|cod\s*m|"
    r"call\s*of\s*duty|fortnite|valorant|wild\s*rift|league\s*of\s*legends|\blol\b|"
    r"dota\s*2|\bbrawl\s*stars|clash\s*royale|minecraft|roblox|\bwot\b|world\s*of\s*tanks|"
    r"arena\s*of\s*valor|\baov\b|clash\s*of\s*clans|among\s*us|csgo|cs2|"
    r"counter\s*strike|apex\s*legends|overwatch|naruto|dragon\s*ball)",
    re.I,
)

NON_MLBB_SPORTS_TITLE = re.compile(
    r"(football|soccer|\bnfl\b|\bnba\b|ncaa|basketball|hockey|tennis|cricket|rugby|"
    r"volleyball|baseball|\bmlb\b(?![\w])|super\s*bowl|premier\s*league|champions\s*league|"
    r"world\s*cup|\bfifa\b|\buefa\b|\bnhl\b|\bmls\b|touchdown|quarterback|goalkeeper|"
    r"penalty\s*kick|wrestling|\bu fc\b|college\s*football|march\s*madness)",
    re.I,
)


def title_rejected_for_mlbb_shorts(text: str) -> str | None:
    """Domain mismatch only — not MLBB vs other games/sports/promo."""
    if PROMO_PATTERNS.search(text):
        return "promo_text"
    if OTHER_GAME_TITLE.search(text):
        return "other_game_title"
    if NON_MLBB_SPORTS_TITLE.search(text):
        return "non_mlbb_sports"
    return None
