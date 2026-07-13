#!/usr/bin/env python3
"""Resolve YouTube title / keywords for an on-disk VOD file."""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

_SAVAGE_RE = re.compile(r"savage|саваж", re.I)
_MANIAC_RE = re.compile(r"maniac|маньяк|ruthless|беспощад", re.I)
_TRIPLE_RE = re.compile(r"triple\s*kill|тройн", re.I)
_DOUBLE_RE = re.compile(r"double\s*kill|двойн", re.I)


def _registry_path() -> Path:
    root = Path(os.environ.get("MLBB_DATA_ROOT", "/root/data/mlbb"))
    return Path(os.environ.get("MLBB_VOD_REGISTRY", str(root / "vod_registry.json")))


def _video_id_from_path(vod: Path) -> str:
    stem = vod.stem
    if stem.startswith("yt_") and len(stem) > 3:
        return stem[3:]
    return stem


def _title_cache_path(video_id: str) -> Path:
    root = Path(os.environ.get("MLBB_DATA_ROOT", "/root/data/mlbb"))
    return root / "title_cache" / f"{video_id}.txt"


def _ytdlp_title(video_id: str) -> str:
    if not video_id or os.environ.get("MLBB_VOD_TITLE_YTDLP", "1") != "1":
        return ""
    cache = _title_cache_path(video_id)
    if cache.exists():
        try:
            return cache.read_text(encoding="utf-8").strip()
        except OSError:
            pass
    try:
        proc = subprocess.run(
            [
                "yt-dlp",
                "--no-playlist",
                "--print",
                "title",
                f"https://www.youtube.com/watch?v={video_id}",
            ],
            capture_output=True,
            text=True,
            timeout=45,
            check=False,
        )
        title = (proc.stdout or "").strip().splitlines()[0] if proc.stdout else ""
        if title and "ERROR" not in title.upper():
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(title, encoding="utf-8")
            return title
    except (OSError, subprocess.TimeoutExpired):
        pass
    return ""


def vod_title_blob(vod: Path, entry: dict | None = None) -> str:
    """Lowercased title + id for keyword gates (savage in title → require savage banner)."""
    env_title = os.environ.get("MLBB_VOD_SCAN_TITLE", "").strip()
    parts: list[str] = [_video_id_from_path(vod), vod.stem]
    if env_title:
        parts.append(env_title)
    if entry and entry.get("title"):
        parts.append(str(entry["title"]))
    else:
        vid = _video_id_from_path(vod)
        reg = _registry_path()
        found = False
        if reg.exists():
            try:
                rows = json.loads(reg.read_text(encoding="utf-8"))
                if isinstance(rows, list):
                    for row in rows:
                        if str(row.get("id", "")) == vid and row.get("title"):
                            parts.append(str(row["title"]))
                            found = True
                            break
            except (json.JSONDecodeError, OSError):
                pass
        if not found:
            yt_title = _ytdlp_title(vid)
            if yt_title:
                parts.append(yt_title)
    return " ".join(parts).lower()


def title_min_banner_tier(blob: str) -> int:
    """When title promises savage/maniac, require matching in-game banner tier."""
    if os.environ.get("MLBB_TITLE_SAVAGE_MIN_TIER", "1") != "1":
        return 0
    if _SAVAGE_RE.search(blob):
        return 5
    if _MANIAC_RE.search(blob):
        return 4
    if _TRIPLE_RE.search(blob):
        return 3
    if _DOUBLE_RE.search(blob):
        return 2
    return 0


def title_promises_kill_streak(blob: str) -> bool:
    return title_min_banner_tier(blob) >= 2


def title_scan_start_sec(blob: str, duration: float) -> float | None:
    """Early start for savage-titled VODs — fights often in first 2–3 min."""
    if not title_promises_kill_streak(blob):
        return None
    base = float(os.environ.get("MLBB_BANNER_SAVAGE_TITLE_START_SEC", "3"))
    if duration <= 240:
        return max(0.0, base)
    return max(0.0, min(base, 15.0))
