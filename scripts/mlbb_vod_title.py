#!/usr/bin/env python3
"""Resolve YouTube title / keywords for an on-disk VOD file."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

_SAVAGE_RE = re.compile(r"savage|саваж|legendary|легендар", re.I)
_MANIAC_RE = re.compile(r"maniac|маньяк|ruthless|беспощад", re.I)
_DOUBLE_RE = re.compile(r"double\s*kill|двойн", re.I)
# Titles that brag about *enemy* streaks should not force our own high-tier gate.
_ENEMY_STREAK_TITLE_RE = re.compile(
    r"(?:enemy|враг|вражеск|противник).{0,48}(?:savage|саваж|maniac|маньяк|legendary|легендар)",
    re.I,
)


def _registry_path() -> Path:
    root = Path(os.environ.get("MLBB_DATA_ROOT", "/root/data/mlbb"))
    return Path(os.environ.get("MLBB_VOD_REGISTRY", str(root / "vod_registry.json")))


def _state_path() -> Path:
    root = Path(os.environ.get("MLBB_DATA_ROOT", "/root/data/mlbb"))
    return Path(os.environ.get("MLBB_VOD_STATE", str(root / "vod_segment_state.json")))


def _video_id_from_path(vod: Path) -> str:
    stem = vod.stem
    if stem.startswith("yt_") and len(stem) > 3:
        return stem[3:]
    return stem


def _title_from_state(vid: str) -> str:
    state = _state_path()
    if not state.exists() or not vid:
        return ""
    try:
        data = json.loads(state.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return ""
    vods = data.get("vods")
    if isinstance(vods, dict):
        entry = vods.get(vid)
        if isinstance(entry, dict) and entry.get("title"):
            return str(entry["title"])
        # Some historical rows used stem/title as key with id inside.
        for _k, row in vods.items():
            if not isinstance(row, dict):
                continue
            if str(row.get("id") or "") == vid and row.get("title"):
                return str(row["title"])
    elif isinstance(vods, list):
        for row in vods:
            if not isinstance(row, dict):
                continue
            if str(row.get("id") or "") == vid and row.get("title"):
                return str(row["title"])
    queue = data.get("title_rescan_queue")
    if isinstance(queue, list):
        for row in queue:
            if isinstance(row, dict) and str(row.get("id") or "") == vid and row.get("title"):
                return str(row["title"])
    return ""


def vod_title_blob(vod: Path, entry: dict | None = None) -> str:
    """Lowercased title + id for keyword gates (savage in title → require savage banner)."""
    parts: list[str] = [_video_id_from_path(vod), vod.stem]
    # Prefer explicit entry title. Only then env / state / registry — never mix a
    # stale MLBB_VOD_SCAN_TITLE from the previous VOD on top of a fresh entry.
    title = ""
    if entry and entry.get("title"):
        title = str(entry["title"])
    if not title:
        title = os.environ.get("MLBB_VOD_SCAN_TITLE", "").strip()
    if not title:
        vid = _video_id_from_path(vod)
        title = _title_from_state(vid)
    if not title:
        reg = _registry_path()
        if reg.exists():
            try:
                rows = json.loads(reg.read_text(encoding="utf-8"))
                if isinstance(rows, list):
                    for row in rows:
                        if str(row.get("id", "")) == vid and row.get("title"):
                            title = str(row["title"])
                            break
            except (json.JSONDecodeError, OSError):
                pass
    if not title:
        # Standalone title_rescan_queue.json (ops helper).
        try:
            qpath = Path(
                os.environ.get("MLBB_TITLE_RESCAN_QUEUE", "/root/data/mlbb/title_rescan_queue.json")
            )
            if qpath.exists():
                payload = json.loads(qpath.read_text(encoding="utf-8"))
                rows = payload.get("queued") if isinstance(payload, dict) else payload
                vid = _video_id_from_path(vod)
                if isinstance(rows, list):
                    for row in rows:
                        if (
                            isinstance(row, dict)
                            and str(row.get("id") or "") == vid
                            and row.get("title")
                        ):
                            title = str(row["title"])
                            break
        except (json.JSONDecodeError, OSError):
            pass
    if title:
        parts.append(title)
    # Prefer on-disk info.json for this exact file when entry title is a stem fallback.
    try:
        info = vod.with_suffix(".info.json")
        if info.exists():
            disk_title = str(json.loads(info.read_text(encoding="utf-8")).get("title") or "")
            if disk_title and disk_title.lower() not in " ".join(parts).lower():
                parts.append(disk_title)
    except (json.JSONDecodeError, OSError):
        pass
    return " ".join(parts).lower()


def title_min_banner_tier(blob: str) -> int:
    """When title promises savage/maniac, require matching in-game banner tier."""
    if os.environ.get("MLBB_TITLE_SAVAGE_MIN_TIER", "1") != "1":
        return 0
    if _ENEMY_STREAK_TITLE_RE.search(blob):
        return 0
    if _SAVAGE_RE.search(blob):
        return 5
    if _MANIAC_RE.search(blob):
        return 4
    if _DOUBLE_RE.search(blob):
        return 2
    return 0


_KILL_COUNT_RE = re.compile(r"\b(\d{1,2})\s*kills?\b", re.I)
_KDA_RE = re.compile(r"\b(\d{1,2})\s*/\s*\d{1,2}\s*/\s*\d{1,2}\b")


def title_kill_count(blob: str) -> int:
    """Best-effort kill count from title ('16 kills' or '20/1/20' KDA)."""
    best = 0
    m = _KILL_COUNT_RE.search(blob or "")
    if m:
        try:
            best = max(best, int(m.group(1)))
        except (TypeError, ValueError):
            pass
    m = _KDA_RE.search(blob or "")
    if m:
        try:
            best = max(best, int(m.group(1)))
        except (TypeError, ValueError):
            pass
    return best


def title_promises_kill_streak(blob: str) -> bool:
    return title_min_banner_tier(blob) >= 2 or title_kill_count(blob) >= 10


def title_scan_start_sec(blob: str, duration: float) -> float | None:
    """Early start for savage-titled or high-kill VODs — fights often in first 2–3 min."""
    kill_n = title_kill_count(blob)
    if not title_promises_kill_streak(blob) and kill_n < 10:
        return None
    base = float(os.environ.get("MLBB_BANNER_SAVAGE_TITLE_START_SEC", "3"))
    if kill_n >= 15:
        base = min(base, 8.0)
    elif kill_n >= 10:
        base = min(base, 12.0)
    if duration <= 240:
        return max(0.0, base)
    if duration <= 900:
        return max(0.0, min(base, 20.0))
    return max(0.0, min(base, 30.0))
