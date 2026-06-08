#!/usr/bin/env python3
"""Game → VK Market product mapping for clip descriptions."""

from __future__ import annotations

import json
from pathlib import Path

REPO_PATH = Path(__file__).resolve().parent.parent / "data" / "vk_game_products.json"
VPS_PATH = Path("/root/content_bot_ml/data/vk_game_products.json")
FALLBACK = Path("/root/data/mlbb/vk_game_products.json")

ALIASES = {
    "mlbb": "mobile_legends",
    "mobile_legends": "mobile_legends",
    "pubg": "pubg",
    "genshin": "genshin",
    "standoff": "standoff",
    "standoff2": "standoff",
    "wot": "wot",
    "world_of_tanks": "wot",
}


def _config_path() -> Path:
    for path in (VPS_PATH, FALLBACK, REPO_PATH):
        if path.exists():
            return path
    return REPO_PATH


def load_products() -> dict:
    path = _config_path()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def normalize_game(game: str) -> str:
    return ALIASES.get(game.strip().lower(), game.strip().lower())


def product_for_game(game: str) -> dict | None:
    row = load_products().get(normalize_game(game)) or {}
    item_id = row.get("market_item_id")
    title = (row.get("title") or "").strip()
    if not item_id and not title:
        return None
    return {"market_item_id": item_id, "title": title}


def description_suffix(game: str = "mobile_legends") -> str:
    row = product_for_game(game)
    if not row:
        return ""
    parts: list[str] = []
    if row.get("title"):
        parts.append(str(row["title"]))
    if row.get("market_item_id"):
        parts.append(f"Товар VK: #{row['market_item_id']}")
    return " | ".join(parts)
