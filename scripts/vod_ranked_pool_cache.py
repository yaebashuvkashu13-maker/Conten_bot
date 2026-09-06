#!/usr/bin/env python3
"""Persist ranked peak pools so multiple montages reuse one ML scan."""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any

POOL_VERSION = 1
DEFAULT_ROOT = "/root/data/pubg/ranked_pool_cache"


def cache_enabled() -> bool:
    return os.environ.get("VOD_RANKED_POOL_CACHE", "1") == "1"


def cache_root() -> Path:
    return Path(os.environ.get("VOD_RANKED_POOL_CACHE_DIR", DEFAULT_ROOT))


def cache_ttl_sec() -> int:
    return max(3600, int(os.environ.get("VOD_RANKED_POOL_CACHE_TTL_SEC", str(24 * 3600))))


def _vod_key(path: Path) -> str:
    p = path.resolve()
    st = p.stat()
    blob = f"v{POOL_VERSION}|{p}|{st.st_mtime_ns}|{st.st_size}"
    return hashlib.sha256(blob.encode("utf-8", errors="replace")).hexdigest()[:32]


def _cache_file(key: str) -> Path:
    return cache_root() / f"{key}.json"


def get_ranked_pool(path: Path) -> dict[str, Any] | None:
    if not cache_enabled() or not path.is_file():
        return None
    key = _vod_key(path)
    cache_file = _cache_file(key)
    if not cache_file.is_file():
        return None
    try:
        payload = json.loads(cache_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    saved_at = float(payload.get("saved_at") or 0)
    if saved_at <= 0 or (time.time() - saved_at) > cache_ttl_sec():
        return None
    if int(payload.get("version") or 0) != POOL_VERSION:
        return None
    return payload


def put_ranked_pool(
    path: Path,
    *,
    ranked_peaks: list[float],
    reason: str = "",
    funnel: dict[str, Any] | None = None,
    used_peaks: list[float] | None = None,
) -> None:
    if not cache_enabled() or not path.is_file() or not ranked_peaks:
        return
    key = _vod_key(path)
    cache_root().mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "version": POOL_VERSION,
        "saved_at": time.time(),
        "vod": str(path.resolve()),
        "ranked_peaks": [round(float(p), 2) for p in ranked_peaks],
        "reason": str(reason)[:500],
        "used_peaks": [round(float(p), 2) for p in (used_peaks or [])],
    }
    if funnel:
        payload["funnel"] = funnel
    cache_file = _cache_file(key)
    tmp = cache_file.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, cache_file)


def unused_peaks(path: Path, used: list[float], *, gap_sec: float = 40.0) -> list[float]:
    """Return ranked peaks not yet consumed for a montage."""
    payload = get_ranked_pool(path)
    if not payload:
        return []
    ranked = [float(p) for p in payload.get("ranked_peaks") or []]
    out: list[float] = []
    for peak in ranked:
        if any(abs(peak - float(u)) < gap_sec for u in used):
            continue
        out.append(peak)
    return out


__all__ = ["get_ranked_pool", "put_ranked_pool", "unused_peaks", "cache_enabled"]
