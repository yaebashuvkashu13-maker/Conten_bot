#!/usr/bin/env python3
"""MLBB ranked VOD discovery: title/channel filters and candidate ranking."""

from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from urllib.parse import quote_plus

# YouTube search UI: "4–20 minutes" duration bucket (closest to our 3–20 min window).
YOUTUBE_DURATION_SP_4_TO_20 = "EgQQARgB"
# YouTube search UI: upload date "This month" — works with yt-dlp on VPS (ytsearchdate does not).
YOUTUBE_FRESHNESS_SP_THIS_MONTH = "EgQIBBAB"

MLBB_VOD_DEFAULT_SEASON = 41
MLBB_VOD_DEFAULT_MAX_AGE_DAYS = 21

# Primary search — broad ranked/match phrases (no global/season lock-in).
VOD_CORE_SEARCH_QUERIES = (
    "MLBB mythic ranked full match gameplay",
    "Mobile Legends mythic glory ranked full game",
    "MLBB legend rank solo queue match replay",
    "Mobile Legends epic mythic ranked teamfight gameplay",
    "MLBB ranked match no montage full game",
    "Mobile Legends global mythic ranked full match",
)

# Role / angle queries — different YouTube result pools from core+hero searches.
VOD_ANGLE_SEARCH_QUERIES = (
    "MLBB mythic placement match full gameplay",
    "Mobile Legends immortal rank push match replay",
    "MLBB roam mythic ranked full game",
    "Mobile Legends jungle mythic ranked match gameplay",
    "MLBB mythic glory savage teamfight ranked match",
    "Mobile Legends ranked match MVP gameplay no commentary",
)

# Kill-heavy titles — primary discovery pool (banner-rich VODs).
VOD_COMBAT_SEARCH_QUERIES = (
    "MLBB ranked match savage teamfight full gameplay",
    "Mobile Legends maniac triple kill ranked full match",
    "MLBB mythic glory double kill teamfight replay",
    "MLBB ranked savage maniac highlights full game no montage",
    "Mobile Legends mythic ranked 20 kills MVP teamfight",
    "MLBB legend ranked triple kill savage gameplay",
    "Mobile Legends double kill teamfight mythic ranked match",
)

# Hero + kill combo — rotated after combat core.
VOD_HERO_COMBAT_TEMPLATES = (
    "MLBB {hero} savage ranked full match gameplay",
    "Mobile Legends {hero} maniac triple kill ranked",
    "MLBB {hero} double kill mythic ranked teamfight",
)

# YouTube upload-date filter: "This week" (alternate pool vs this month).
YOUTUBE_FRESHNESS_SP_THIS_WEEK = "EgQIARAB"

# Popular carry/fight heroes for rotating VOD search (tanks/supports excluded).
from mlbb_hero_roles import highlight_search_heroes

VOD_SEARCH_HEROES = highlight_search_heroes()

# Hard reject — montages, guides, promos, skin showcases.
BAD_TITLE_RE = re.compile(
    r"(?:"
    r"giveaway|#short\b|shorts\b|tiktok\b|reels?\b|"
    r"montage|compilation|compilación|highlight(?:s)?\s+reel|best\s+(?:moment|play)|"
    r"top\s+\d+\s+(?:play|moment|savage|best|heroes?|junglers?|mid|roam|exp|gold)|"
    r"savage\s+montage|"
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
    r"\b(?:ranked?|mythic|mythical|legend|epic|grandmaster|immortal|solo\s*queue?|"
    r"match|gameplay|full\s+(?:game|match)|replay|vs\.?|global|placement|"
    r"mvp|teamfight|glory)\b",
    re.I,
)

MLBB_EXPLICIT_TITLE_RE = re.compile(
    r"mobile legends|mlbb|bang bang|мобайл легенд|#mlbb|#mobilelegends",
    re.I,
)

MLBB_IMPLICIT_TITLE_RE = re.compile(
    r"mythic glory|mythical glory|mythic ranked|ranked game|ranked match|"
    r"mythical placement|mythic placement|immortal rank|legend rank|"
    r"epic rank|solo rank|rank push",
    re.I,
)

GUIDE_TITLE_RE = re.compile(
    r"(?:"
    r"best\s+\d+\s+heroes|heroes\s+for\s+every\s+role|for\s+every\s+role|"
    r"best\s+solo\s+carry\s+heroes|tier\s+list|hero\s+tier|"
    r"how\s+to\s+rank|rank\s+guide|emblem\s+setup|item\s+build\s+guide|"
    r"wr\s+build|meta\s+build|passive\s+skill|skill\s+combo\s+guide|"
    # Listicles / meta roundups that waste download quota (no real match HUD).
    r"top\s+\d+\s+(?:best\s+)?(?:heroes?|junglers?|fighters?|mages?|assassins?|"
    r"marksmen|supports?|tanks?|roamers?|mid(?:laners?)?|exp|gold)|"
    r"most\s+picked|"
    r"best\s+heroes?\s+to\s+(?:use|rank|play)|"
    r"easy\s+rank\s+push\s+heroes|"
    r"heroes?\s+above\s+(?:mythical|mythic|immortal)|"
    r"short\s+guide|(?:hero\s+)?guide\s+for|until\s+you\s+watch|"
    r"don't\s+use\s+\w+\s+until|do\s+not\s+use\s+\w+\s+until"
    r")",
    re.I,
)


def vod_current_season() -> int:
    raw = (os.environ.get("MLBB_VOD_SEASON") or "").strip()
    if raw.isdigit():
        return int(raw)
    return MLBB_VOD_DEFAULT_SEASON


def vod_max_age_days(env: dict[str, str] | None = None) -> int:
    merged = {**os.environ, **(env or {})}
    raw = (merged.get("MLBB_VOD_MAX_AGE_DAYS") or "").strip()
    if raw.isdigit():
        return int(raw)
    return MLBB_VOD_DEFAULT_MAX_AGE_DAYS


def vod_search_date_sort(env: dict[str, str] | None = None) -> bool:
    """Prefer YouTube upload-date filter + post-filter for fresher candidates."""
    merged = {**os.environ, **(env or {})}
    return merged.get("MLBB_VOD_SEARCH_FRESH", "1") != "0"


def vod_youtube_freshness_sp(env: dict[str, str] | None = None) -> str:
    merged = {**os.environ, **(env or {})}
    if not vod_search_date_sort(merged):
        return ""
    explicit = (merged.get("MLBB_VOD_YOUTUBE_FRESHNESS_SP") or "").strip()
    if explicit.lower() in ("0", "off", "none", "disable", "disabled"):
        return ""
    if explicit:
        return explicit
    return YOUTUBE_FRESHNESS_SP_THIS_MONTH


def vod_search_include_supplements(env: dict[str, str] | None = None) -> bool:
    merged = {**os.environ, **(env or {})}
    return merged.get("MLBB_VOD_SEARCH_SUPPLEMENT", "1") != "0"


def build_vod_search_queries(
    *,
    season: int | None = None,
    heroes: tuple[str, ...] | None = None,
    max_hero_queries: int = 8,
    include_supplements: bool | None = None,
    limit: int = 20,
) -> list[str]:
    """Combat-first query list: savage/maniac/double before generic ranked."""
    if include_supplements is None:
        include_supplements = vod_search_include_supplements()
    heroes = heroes or VOD_SEARCH_HEROES
    season = season if season is not None else vod_current_season()
    queries: list[str] = list(VOD_COMBAT_SEARCH_QUERIES)

    hero_slots = max(0, min(max_hero_queries, max(0, limit - 6)))
    for idx in range(hero_slots):
        hero = heroes[idx % len(heroes)]
        tpl = VOD_HERO_COMBAT_TEMPLATES[idx % len(VOD_HERO_COMBAT_TEMPLATES)]
        queries.append(tpl.format(hero=hero))

    # Small generic ranked tail — not the main pool anymore.
    for q in VOD_CORE_SEARCH_QUERIES[:2]:
        if len(queries) >= limit:
            break
        queries.append(q)

    for angle in VOD_ANGLE_SEARCH_QUERIES:
        if len(queries) >= limit:
            break
        # Skip roam-biased angles — low kill-banner yield.
        if "roam" in angle.lower():
            continue
        queries.append(angle)

    if include_supplements and len(queries) < limit:
        queries.append(f"MLBB mythic global savage season {season} ranked full match")
    if include_supplements and len(queries) < limit:
        queries.append(f"MLBB mythic glory double kill season {season} ranked gameplay")
    # Dedupe while preserving order.
    seen: set[str] = set()
    out: list[str] = []
    for q in queries:
        key = q.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(q)
        if len(out) >= limit:
            break
    return out


def pick_vod_search_batch(queries: list[str], offset: int, batch_size: int) -> tuple[list[str], int]:
    """Round-robin slice — each pass hits a different query subset."""
    if not queries:
        return [], 0
    batch_size = max(1, min(batch_size, len(queries)))
    offset = offset % len(queries)
    batch = [queries[(offset + i) % len(queries)] for i in range(batch_size)]
    return batch, (offset + batch_size) % len(queries)


def vod_discovery_search_cycle(cycle: int, env: dict[str, str] | None = None) -> dict[str, object]:
    """
    Rotate YouTube filters so we don't always scrape the same 'this month' page.
    cycle 0: upload this month | cycle 1: 4–20 min (broader age) | cycle 2: this week
    """
    merged = {**os.environ, **(env or {})}
    mode = int(cycle) % 3
    if mode == 0:
        return {
            "youtube_search_date": True,
            "youtube_freshness_sp": vod_youtube_freshness_sp(merged) or YOUTUBE_FRESHNESS_SP_THIS_MONTH,
            "youtube_duration_sp": "",
            "max_age_days": vod_max_age_days(merged),
        }
    if mode == 1:
        return {
            "youtube_search_date": False,
            "youtube_freshness_sp": "",
            "youtube_duration_sp": vod_youtube_duration_sp({**merged, "MLBB_VOD_SEARCH_FRESH": "0"}),
            "max_age_days": max(vod_max_age_days(merged), 35),
        }
    return {
        "youtube_search_date": True,
        "youtube_freshness_sp": YOUTUBE_FRESHNESS_SP_THIS_WEEK,
        "youtube_duration_sp": "",
        "max_age_days": vod_max_age_days(merged),
    }


def passes_mlbb_game_title(title: str) -> bool:
    """Accept ranked VOD titles even when uploader omits 'MLBB' in the name."""
    blob = str(title or "")
    # Guides/listicles often append "| MLBB" — reject before the explicit brand pass.
    if GUIDE_TITLE_RE.search(blob) or BAD_TITLE_RE.search(blob):
        return False
    if MLBB_EXPLICIT_TITLE_RE.search(blob):
        return True
    return bool(MLBB_IMPLICIT_TITLE_RE.search(blob) and RANKED_SIGNAL_RE.search(blob))


def default_vod_search_queries_csv() -> str:
    return ",".join(build_vod_search_queries())


DEFAULT_SEARCH_QUERIES = default_vod_search_queries_csv()


def parse_upload_date_ymd(raw: str) -> str:
    text = str(raw or "").strip()
    if len(text) >= 8 and text[:8].isdigit():
        return text[:8]
    return ""


def upload_age_days(upload_date: str, *, now: datetime | None = None) -> int | None:
    ymd = parse_upload_date_ymd(upload_date)
    if not ymd:
        return None
    try:
        uploaded = datetime.strptime(ymd, "%Y%m%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    ref = now or datetime.now(timezone.utc)
    return max(0, (ref.date() - uploaded.date()).days)


def passes_upload_freshness(meta: dict, *, max_age_days: int | None = None) -> bool:
    limit = MLBB_VOD_DEFAULT_MAX_AGE_DAYS if max_age_days is None else max_age_days
    if limit <= 0:
        return True
    age = upload_age_days(str(meta.get("upload_date") or ""))
    if age is None:
        return True
    return age <= limit


def youtube_results_search_url(query: str, *, duration_sp: str = "") -> str:
    """YouTube results URL; optional sp= applies the site duration filter (not query text)."""
    url = f"https://www.youtube.com/results?search_query={quote_plus(query)}"
    sp = (duration_sp or "").strip()
    if sp:
        url += f"&sp={sp}"
    return url


def vod_youtube_duration_sp(env: dict[str, str] | None = None) -> str:
    merged = {**os.environ, **(env or {})}
    if vod_search_date_sort(merged):
        return ""
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
    title = str(meta.get("title") or "")
    if BAD_TITLE_RE.search(blob):
        return False
    if GUIDE_TITLE_RE.search(blob):
        return False
    if SOFT_BAD_TITLE_RE.search(blob) and not RANKED_SIGNAL_RE.search(blob):
        return False
    if not passes_mlbb_game_title(title):
        return False
    try:
        from mlbb_hero_roles import title_is_tank_support_only

        if title_is_tank_support_only(title):
            return False
    except Exception:
        pass
    return True


def rank_mlbb_vod_candidate(meta: dict, *, target_dur_sec: float = 780.0) -> float:
    """Higher score = better candidate for ranked fight extraction."""
    blob = text_blob(meta).lower()
    dur = float(meta.get("duration") or 0)
    score = 0.0

    score -= abs(dur - target_dur_sec) / 180.0

    age = upload_age_days(str(meta.get("upload_date") or ""))
    if age is not None:
        if age <= 2:
            score += 9.0
        elif age <= 7:
            score += 6.0
        elif age <= 14:
            score += 3.0
        elif age <= vod_max_age_days():
            score += 1.0
        else:
            score -= 12.0

    boosts = (
        ("full match", 5.0),
        ("full game", 5.0),
        ("ranked", 4.0),
        ("mythic", 4.0),
        ("legend", 3.0),
        ("immortal", 3.0),
        ("grandmaster", 2.5),
        ("global", 2.0),
        ("solo queue", 3.0),
        ("solo rank", 3.0),
        ("gameplay", 2.0),
        ("replay", 2.5),
        ("match", 2.0),
        (" vs ", 2.5),
        ("savage", 3.5),
        ("maniac", 3.0),
        ("triple kill", 3.0),
        ("double kill", 2.5),
        ("teamfight", 2.0),
        (" kills", 2.0),
        ("mvp", 1.5),
        ("no commentary", 1.0),
        (f"season {vod_current_season()}", 1.5),
    )
    for needle, weight in boosts:
        if needle in blob:
            score += weight

    penalties = (
        ("montage", -12.0),
        ("compilation", -12.0),
        ("highlight", -8.0),
        ("full highlights", -10.0),
        ("best moment", -8.0),
        ("best build", -4.0),
        ("for every role", -14.0),
        ("best solo carry", -14.0),
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
        ("wr ", -5.0),
        ("meta build", -5.0),
        ("passive", -4.0),
        ("macro only", -6.0),
        ("farm", -3.0),
        ("unbox", -8.0),
        ("preview", -5.0),
    )
    for needle, weight in penalties:
        if needle in blob:
            score += weight

    try:
        from mlbb_hero_roles import heroes_in_text, is_excluded_role, is_highlight_role

        title_heroes = heroes_in_text(blob)
        if title_heroes and all(is_excluded_role(h) for h in title_heroes):
            score -= 18.0
        elif any(is_highlight_role(h) for h in title_heroes):
            score += 4.0
    except Exception:
        pass

    return score


def normalize_uploader(meta: dict) -> str:
    return str(meta.get("uploader") or meta.get("channel") or "").strip().casefold()
