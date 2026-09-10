#!/usr/bin/env python3
"""In-process cache for kill-notification scoring — avoids duplicate ffmpeg decodes."""

from __future__ import annotations

from pathlib import Path
from typing import Any

_CACHE: dict[tuple[str, int, float, float], tuple[float, dict[str, Any]]] = {}
_MAX = 512


def _key(video_path: Path, start_sec: float, duration_sec: float) -> tuple[str, int, float, float]:
    stat = video_path.stat()
    return (
        str(video_path.resolve()),
        stat.st_mtime_ns,
        round(float(start_sec), 1),
        round(float(duration_sec), 1),
    )


def get_cached(
    video_path: Path,
    start_sec: float,
    duration_sec: float,
) -> tuple[float, dict[str, Any]] | None:
    return _CACHE.get(_key(video_path, start_sec, duration_sec))


def cached_score_kill_notification_segment(
    video_path: Path,
    start_sec: float,
    duration_sec: float,
) -> tuple[float, dict[str, Any]]:
    key = _key(video_path, start_sec, duration_sec)
    hit = _CACHE.get(key)
    if hit is not None:
        score, meta = hit
        meta = dict(meta)
        meta["notification_cache_hit"] = True
        return score, meta
    from pubg_kill_notification import score_kill_notification_segment

    score, meta = score_kill_notification_segment(video_path, start_sec, duration_sec)
    if len(_CACHE) >= _MAX:
        _CACHE.clear()
    _CACHE[key] = (score, dict(meta))
    return score, meta


def clear_notification_cache() -> None:
    _CACHE.clear()


__all__ = [
    "cached_score_kill_notification_segment",
    "clear_notification_cache",
    "get_cached",
]
