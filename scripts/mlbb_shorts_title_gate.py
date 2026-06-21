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

SPAM_SHORTS_TITLE = re.compile(
    r"(#shortlive\b|#shortsfeed\b|#viralshorts\b|#foryoupage\b)",
    re.I,
)

GENERIC_CLICKBAIT = re.compile(
    r"^(hmmm+!?|wow+!?|omg+!?|wait+!?|pov\b|listen\b|bro\b|no\s*way\b)[\s!?.#]*",
    re.I,
)

MLBB_TITLE_HINT = re.compile(
    r"(mlbb|mobile\s*legends|savage|mythic|mpl|m[1-7]\b|teamfight|ranked|"
    r"hero|ling|fanny|chou|gusion|franco|tigreal|hayabusa|lancelot|"
    r"kill|fight|clutch|montage|gameplay|esports|onic|alter\s*ego|rrq|benedetta|"
    r"paquito|beatrix|natan|yin|valentina|phoveus|brody|claude)",
    re.I,
)


def title_rejected_for_mlbb_shorts(text: str) -> str | None:
    """Fast title-only reject before download. Returns reason code or None."""
    if PROMO_PATTERNS.search(text):
        return "promo_text"
    if OTHER_GAME_TITLE.search(text):
        return "other_game_title"
    if NON_MLBB_SPORTS_TITLE.search(text):
        return "non_mlbb_sports"
    if SPAM_SHORTS_TITLE.search(text):
        return "spam_shorts_tag"
    stripped = text.strip()
    if GENERIC_CLICKBAIT.search(stripped) and not MLBB_TITLE_HINT.search(text):
        return "generic_clickbait"
    alpha_words = [w for w in re.findall(r"[a-z]{3,}", text.lower()) if w not in {"short", "shorts", "live"}]
    hashtags = re.findall(r"#\w+", text.lower())
    if not MLBB_TITLE_HINT.search(text) and hashtags and len(alpha_words) <= 1:
        return "no_mlbb_title"
    return None
