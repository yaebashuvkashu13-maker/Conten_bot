#!/usr/bin/env python3
"""Disk cache for analyze_video() — invalidate by path + mtime_ns + size."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

import numpy as np

DEFAULT_ROOT = "/root/data/vod_analysis_cache"
_ARRAY_KEYS = ("motion", "center_motion", "audio", "scene", "gunfire")


def cache_root() -> Path:
    return Path(os.environ.get("VOD_ANALYSIS_CACHE_DIR", DEFAULT_ROOT))


def cache_key_tuple(path: Path) -> tuple[str, int, int]:
    p = path.resolve()
    st = p.stat()
    return str(p), int(st.st_mtime_ns), int(st.st_size)


def cache_key_hash(path: Path) -> str:
    path_s, mtime_ns, size = cache_key_tuple(path)
    blob = f"{path_s}|{mtime_ns}|{size}".encode("utf-8", errors="replace")
    return hashlib.sha256(blob).hexdigest()[:32]


def _cache_path(path: Path) -> Path:
    return cache_root() / f"{cache_key_hash(path)}.json"


def _arrays_to_lists(analysis: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {
        "duration": float(analysis.get("duration") or 0.0),
        "bins": int(analysis.get("bins") or 0),
        "window_seconds": float(analysis.get("window_seconds") or 2.0),
    }
    for key in _ARRAY_KEYS:
        val = analysis.get(key)
        if val is None:
            continue
        out[key] = np.asarray(val, dtype=np.float32).tolist()
    if "gunfire" not in out and "audio" in out:
        out["gunfire"] = out["audio"]
    return out


def _lists_to_arrays(payload: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {
        "duration": float(payload.get("duration") or 0.0),
        "bins": int(payload.get("bins") or 0),
        "window_seconds": float(payload.get("window_seconds") or 2.0),
    }
    for key in _ARRAY_KEYS:
        if key not in payload:
            continue
        out[key] = np.asarray(payload[key], dtype=np.float32)
    if "gunfire" not in out and "audio" in out:
        out["gunfire"] = out["audio"]
    return out


def get_cached(path: Path) -> dict[str, Any] | None:
    """Return cached analysis or None on miss / stale."""
    p = path.resolve()
    if not p.exists():
        return None
    cache_file = _cache_path(p)
    if not cache_file.exists():
        return None
    try:
        payload = json.loads(cache_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    key = payload.get("key") or {}
    cur_path, cur_mtime, cur_size = cache_key_tuple(p)
    if (
        key.get("path") != cur_path
        or int(key.get("mtime_ns") or 0) != cur_mtime
        or int(key.get("size") or 0) != cur_size
    ):
        return None
    return _lists_to_arrays(payload.get("analysis") or {})


def set_cached(path: Path, analysis: dict[str, Any]) -> str:
    """Persist slim analysis; returns cache key hash."""
    p = path.resolve()
    cache_root().mkdir(parents=True, exist_ok=True)
    key_hash = cache_key_hash(p)
    path_s, mtime_ns, size = cache_key_tuple(p)
    payload = {
        "key": {"path": path_s, "mtime_ns": mtime_ns, "size": size},
        "analysis": _arrays_to_lists(analysis),
    }
    cache_file = cache_root() / f"{key_hash}.json"
    cache_file.write_text(json.dumps(payload), encoding="utf-8")
    return key_hash


def analysis_source_path(path: Path) -> Path:
    """Prefer H.264 proxy for decode-heavy analyze_video when enabled."""
    if os.environ.get("VOD_ANALYSIS_USE_PROXY", "1") != "1":
        return path
    proxy = path.parent / f"{path.stem}_h264.mp4"
    if proxy.exists() and proxy.stat().st_size > 1024:
        return proxy
    return path


def analyze_video_cached(path: Path) -> dict[str, Any]:
    """Cached analyze_video — proxy path for analysis, original for render elsewhere."""
    src = analysis_source_path(path)
    cached = get_cached(src)
    if cached is not None:
        return cached
    from smart_video_editor import analyze_video

    analysis = analyze_video(src)
    try:
        set_cached(src, analysis)
    except OSError:
        pass
    return analysis


def clear_memory_cache() -> None:
    """No-op placeholder for callers that cleared in-process caches."""
    return None
