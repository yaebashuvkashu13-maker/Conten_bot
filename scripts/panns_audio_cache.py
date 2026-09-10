#!/usr/bin/env python3
"""Disk cache for score_panns_audio() windows — keyed by VOD identity + offset."""

from __future__ import annotations

import hashlib
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

DEFAULT_ROOT = "/root/data/panns_audio_cache"
CACHE_VERSION = 1


def cache_enabled() -> bool:
    return os.environ.get("PANN_AUDIO_CACHE", "1") == "1"


def cache_root() -> Path:
    return Path(os.environ.get("PANN_AUDIO_CACHE_DIR", DEFAULT_ROOT))


def cache_ttl_sec() -> int:
    return max(3600, int(os.environ.get("PANN_AUDIO_CACHE_TTL_SEC", str(7 * 86400))))


def _vod_key(path: Path) -> tuple[str, int, int]:
    p = path.resolve()
    st = p.stat()
    return str(p), int(st.st_mtime_ns), int(st.st_size)


def window_key(path: Path, start_sec: float, duration_sec: float) -> str:
    path_s, mtime_ns, size = _vod_key(path)
    blob = f"v{CACHE_VERSION}|{path_s}|{mtime_ns}|{size}|{round(float(start_sec), 2)}|{round(float(duration_sec), 2)}"
    return hashlib.sha256(blob.encode("utf-8", errors="replace")).hexdigest()[:32]


def _cache_file(key: str) -> Path:
    return cache_root() / f"{key}.json"


def get_cached(path: Path, start_sec: float, duration_sec: float) -> dict[str, float] | None:
    if not cache_enabled() or not path.is_file():
        return None
    cache_file = _cache_file(window_key(path, start_sec, duration_sec))
    if not cache_file.is_file():
        return None
    try:
        payload = json.loads(cache_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    saved_at = float(payload.get("saved_at") or 0)
    if saved_at <= 0 or (time.time() - saved_at) > cache_ttl_sec():
        return None
    scores = payload.get("scores")
    if not isinstance(scores, dict):
        return None
    return {str(k): float(v) for k, v in scores.items()}


def put_cached(path: Path, start_sec: float, duration_sec: float, scores: dict[str, float]) -> None:
    if not cache_enabled() or not path.is_file():
        return
    root = cache_root()
    root.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "saved_at": time.time(),
        "vod": str(path.resolve()),
        "start_sec": round(float(start_sec), 2),
        "duration_sec": round(float(duration_sec), 2),
        "scores": {str(k): float(v) for k, v in scores.items()},
    }
    cache_file = _cache_file(window_key(path, start_sec, duration_sec))
    cache_file.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def prewarm_grid(path: Path, offsets: list[float], window_sec: float) -> int:
    """Ensure every probe window is cached (one ffmpeg+PANN pass per offset)."""
    from highlight_scorer import score_panns_audio

    workers = max(
        1,
        int(
            os.environ.get(
                "PANN_PREWARM_WORKERS",
                os.environ.get("HIGHLIGHT_PARALLEL_WORKERS", "1"),
            )
        ),
    )
    if workers == 1 or len(offsets) < 2:
        for t in offsets:
            score_panns_audio(path, t, window_sec)
    else:
        with ThreadPoolExecutor(max_workers=min(workers, len(offsets))) as pool:
            list(pool.map(lambda t: score_panns_audio(path, t, window_sec), offsets))
    return len(offsets)
