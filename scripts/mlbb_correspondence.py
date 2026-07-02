#!/usr/bin/env python3
"""
MLBB correspondence — does a YouTube result match our MLBB search intent?

We only search MLBB query pool. Before download, title must correspond to that query
(football found via "mlbb savage" → no correspondence). Not a reactive blocklist.
"""

from __future__ import annotations

import json
import os
import re
from functools import lru_cache
from pathlib import Path

from mlbb_shorts_title_gate import NON_MLBB_SPORTS_TITLE, OTHER_GAME_TITLE, PROMO_PATTERNS

_STOP = frozenset(
    {
        "short",
        "shorts",
        "gameplay",
        "highlights",
        "highlight",
        "video",
        "mobile",
        "legends",
        "the",
        "with",
        "from",
        "this",
        "that",
        "your",
        "best",
        "watch",
        "full",
        "clip",
        "january",
        "february",
        "march",
        "april",
        "2024",
        "2025",
        "2026",
    }
)

# Anchors shared across SEARCH_QUERIES — positive MLBB domain vocabulary.
_MLBB_DOMAIN = frozenset(
    {
        "mlbb",
        "savage",
        "mythic",
        "mpl",
        "teamfight",
        "ranked",
        "esports",
        "onic",
        "alter",
        "ego",
        "rrq",
        "triple",
        "kill",
        "fight",
        "chou",
        "fanny",
        "ling",
        "gusion",
        "franco",
        "tigreal",
        "hayabusa",
        "lancelot",
        "benedetta",
        "paquito",
        "beatrix",
        "natan",
        "yin",
        "valentina",
        "phoveus",
        "brody",
        "claude",
        "montage",
        "clutch",
    }
)


def _tokenize(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z]{3,}", text.lower()) if w not in _STOP}


@lru_cache(maxsize=1)
def mlbb_domain_tokens() -> frozenset[str]:
    tokens = set(_MLBB_DOMAIN)
    repo = Path(os.environ.get("CONTENT_BOT_REPO", "/root/content_bot_ml"))
    heroes_path = repo / "config" / "mlbb_heroes.json"
    if heroes_path.exists():
        try:
            data = json.loads(heroes_path.read_text(encoding="utf-8"))
            for hero in data.get("heroes", []):
                for tag in hero.get("tags") or []:
                    for part in re.findall(r"[a-z]{3,}", str(tag).lower()):
                        if part not in _STOP:
                            tokens.add(part)
                name = str(hero.get("name", "")).lower()
                for part in re.findall(r"[a-z]{3,}", name):
                    if part not in _STOP:
                        tokens.add(part)
        except (json.JSONDecodeError, OSError):
            pass
    return frozenset(tokens)


def corresponds_to_mlbb_search(*, title: str, search_query: str) -> tuple[bool, str]:
    """
  True when a search hit plausibly belongs to the MLBB query that found it.
  football + query "mlbb savage shorts" → False (domain_conflict).
  "Hmmm #shortlive" + same query → False (no_correspondence).
  "Chou savage mlbb" + query "mlbb chou savage shorts" → True.
    """
    if PROMO_PATTERNS.search(title):
        return False, "promo_not_mlbb"
    if OTHER_GAME_TITLE.search(title) or NON_MLBB_SPORTS_TITLE.search(title):
        return False, "domain_conflict"

    query = str(search_query or "").strip()
    if not query:
        # Disk refill / unknown provenance — title must carry MLBB domain itself.
        if _tokenize(title) & mlbb_domain_tokens():
            return True, "mlbb_domain"
        return False, "no_correspondence"

    title_toks = _tokenize(title)
    query_toks = _tokenize(query)
    domain = mlbb_domain_tokens()

    if title_toks & domain:
        return True, "mlbb_domain"

    overlap = title_toks & query_toks
    if overlap:
        return True, f"query_overlap:{sorted(overlap)[0]}"

    if query_toks & domain:
        return False, "no_correspondence"

    return False, "no_correspondence"


def passes_owner_video_correspondence(path: Path, profile: str = "mobile_legends") -> tuple[bool, float]:
    """After download: clip similarity to owner 👍/👎 exemplars (learned correspondence)."""
    from mlbb_calibration_store import owner_rank_enabled

    if not owner_rank_enabled():
        return True, float("nan")
    try:
        from mlbb_calibration_store import compute_owner_score
    except ImportError:
        return True, float("nan")
    score = compute_owner_score(path)
    if score != score:
        return True, score
    floor = float(os.environ.get("MLBB_OWNER_SCORE_MIN", "-0.08"))
    return float(score) >= floor, score
