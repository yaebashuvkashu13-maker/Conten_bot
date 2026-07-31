#!/usr/bin/env python3
"""MLBB hero roles — discovery/search filtering and title hero parsing."""

from __future__ import annotations

import json
import os
import re
from functools import lru_cache
from pathlib import Path


def _heroes_json_path() -> Path:
    env = os.environ.get("MLBB_HEROES_JSON", "").strip()
    if env:
        return Path(env)
    repo = os.environ.get("CONTENT_BOT_REPO", "").strip()
    if repo:
        cand = Path(repo) / "config" / "mlbb_heroes.json"
        if cand.exists():
            return cand
    return Path(__file__).resolve().parent.parent / "config" / "mlbb_heroes.json"


@lru_cache(maxsize=1)
def load_heroes() -> tuple[dict, ...]:
    path = _heroes_json_path()
    if not path.exists():
        return tuple()
    data = json.loads(path.read_text(encoding="utf-8"))
    return tuple(data.get("heroes") or [])


def hero_role(hero_id: str) -> str:
    hid = str(hero_id or "").strip().lower()
    for row in load_heroes():
        if str(row.get("id") or "").lower() == hid:
            return str(row.get("role") or "fighter").lower()
    return "fighter"


def is_excluded_role(hero_id: str) -> bool:
    """Tank/support VODs rarely produce spectacle highlights."""
    if os.environ.get("MLBB_VOD_SKIP_TANK_SUPPORT", "1") != "1":
        return False
    return hero_role(hero_id) in {"tank", "support"}


def is_highlight_role(hero_id: str) -> bool:
    return not is_excluded_role(hero_id)


def hero_from_text(text: str) -> str | None:
    blob = str(text or "").lower()
    if not blob:
        return None
    best: tuple[int, str] | None = None
    for row in load_heroes():
        hid = str(row.get("id") or "")
        for tag in row.get("tags") or []:
            tag_l = str(tag).lower()
            if not tag_l:
                continue
            if re.search(rf"\b{re.escape(tag_l)}\b", blob):
                score = len(tag_l)
                if best is None or score > best[0]:
                    best = (score, hid)
    return best[1] if best else None


def heroes_in_text(text: str) -> list[str]:
    blob = str(text or "").lower()
    found: list[str] = []
    for row in load_heroes():
        hid = str(row.get("id") or "")
        for tag in row.get("tags") or []:
            tag_l = str(tag).lower()
            if tag_l and re.search(rf"\b{re.escape(tag_l)}\b", blob):
                found.append(hid)
                break
    return list(dict.fromkeys(found))


def title_is_tank_support_only(title: str) -> bool:
    heroes = heroes_in_text(title)
    if not heroes:
        return False
    return all(is_excluded_role(h) for h in heroes)


def highlight_search_heroes() -> tuple[str, ...]:
    """Carry/fight heroes for YouTube discovery rotation."""
    out: list[str] = []
    for row in load_heroes():
        hid = str(row.get("id") or "")
        if not hid:
            continue
        if is_highlight_role(hid):
            out.append(hid)
    if out:
        return tuple(out)
    # Fallback if config missing roles.
    return (
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
        "layla",
        "kagura",
        "lancelot",
        "dyrroth",
        "benedetta",
    )


def played_hero_from_vod(vod: Path, *, title: str = "") -> str | None:
    blob = title
    if not blob:
        try:
            from mlbb_vod_title import vod_title_blob

            blob = vod_title_blob(vod)
        except Exception:
            blob = vod.stem
    return hero_from_text(blob)
