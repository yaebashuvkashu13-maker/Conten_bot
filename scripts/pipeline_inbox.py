#!/usr/bin/env python3
"""Resolve montage source videos from the nightly inbox."""

from __future__ import annotations

import re
from pathlib import Path

INBOX = Path("/root/data/mlbb/youtube_nightly/inbox")


def youtube_id_from_name(name: str) -> str | None:
    stem = Path(name).stem
    if "_youtube_" in stem:
        return stem.rsplit("_youtube_", 1)[-1]
    if stem.startswith("yt_"):
        return stem[3:]
    match = re.search(r"(?:_youtube_|^yt_)([A-Za-z0-9_-]{6,})$", stem)
    return match.group(1) if match else None


def other_game_source_names(games: list[dict], game_id: str) -> set[str]:
    names: set[str] = set()
    for game in games:
        if game.get("id") == game_id:
            continue
        for name in game.get("sources") or []:
            names.add(name)
    return names


def inbox_sources_for_game(
    game: dict,
    *,
    inbox: Path = INBOX,
    all_games: list[dict] | None = None,
) -> list[Path]:
    """
    Explicit game sources first, then any other inbox VODs (newest mtime first).

    For Standoff this picks up freshly uploaded streams without editing the sources list.
    """
    explicit = [inbox / name for name in (game.get("sources") or [])]
    explicit = [p for p in explicit if p.exists()]

    extra: list[Path] = []
    if str(game.get("id", "")) == "standoff":
        claimed = other_game_source_names(all_games or [], "standoff")
        for path in sorted(inbox.glob("*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True):
            if path.name in claimed:
                continue
            if path in explicit:
                continue
            extra.append(path)

    seen: set[str] = set()
    ordered: list[Path] = []
    for path in explicit + extra:
        if path.name in seen:
            continue
        seen.add(path.name)
        ordered.append(path)
    return ordered


def pick_inbox_source(
    game: dict,
    attempt: int,
    *,
    inbox: Path = INBOX,
    all_games: list[dict] | None = None,
) -> Path | None:
    sources = inbox_sources_for_game(game, inbox=inbox, all_games=all_games)
    if not sources:
        return None
    return sources[min(max(attempt - 1, 0), len(sources) - 1)]
