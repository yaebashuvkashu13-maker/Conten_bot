#!/usr/bin/env python3
"""Disk cache for fast-montage discovery features — reuse without re-decode."""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any

DEFAULT_ROOT = "/root/data/vod_peak_feature_cache"
# v3: key includes dense probe step so 5s→1s grids do not reuse stale peaks.
CACHE_VERSION = 3


def cache_enabled() -> bool:
    return os.environ.get("VOD_PEAK_FEATURE_CACHE", "1") == "1"


def cache_root() -> Path:
    return Path(os.environ.get("VOD_PEAK_FEATURE_CACHE_DIR", DEFAULT_ROOT))


def cache_ttl_sec() -> int:
    return max(3600, int(os.environ.get("VOD_PEAK_FEATURE_CACHE_TTL_SEC", str(6 * 3600))))


def _probe_step_token() -> str:
    raw = os.environ.get("SHOOTER_VOD_DENSE_PROBE_STEP_SEC", "1")
    try:
        step = float(raw)
    except (TypeError, ValueError):
        step = 1.0
    if step <= 0:
        step = 1.0
    return f"{step:.3f}"


def _vod_key(path: Path) -> tuple[str, int, int]:
    p = path.resolve()
    st = p.stat()
    return str(p), int(st.st_mtime_ns), int(st.st_size)


def cache_key(path: Path, probe_pass: int) -> str:
    path_s, mtime_ns, size = _vod_key(path)
    blob = (
        f"v{CACHE_VERSION}|{path_s}|{mtime_ns}|{size}"
        f"|pass={probe_pass}|step={_probe_step_token()}"
    )
    return hashlib.sha256(blob.encode("utf-8", errors="replace")).hexdigest()[:32]


def _cache_file(key: str) -> Path:
    return cache_root() / f"{key}.json"


def get_cached(path: Path, probe_pass: int) -> dict[str, Any] | None:
    if not cache_enabled() or not path.is_file():
        return None
    cache_file = _cache_file(cache_key(path, probe_pass))
    if not cache_file.is_file():
        return None
    try:
        payload = json.loads(cache_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    saved_at = float(payload.get("saved_at") or 0)
    if saved_at <= 0 or (time.time() - saved_at) > cache_ttl_sec():
        return None
    return payload


def put_cached(
    path: Path,
    probe_pass: int,
    *,
    peaks: list[float],
    reason: str,
    features: list[dict[str, Any]] | None = None,
    funnel: dict[str, Any] | None = None,
    timings: dict[str, float | int] | None = None,
) -> None:
    if not cache_enabled() or not path.is_file():
        return
    root = cache_root()
    root.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "saved_at": time.time(),
        "vod": str(path.resolve()),
        "probe_pass": int(probe_pass),
        "peaks": [round(float(p), 1) for p in peaks],
        "reason": str(reason),
    }
    if features:
        payload["features"] = features
    if funnel:
        payload["funnel"] = funnel
    if timings:
        payload["timings_ms"] = timings
    key = cache_key(path, probe_pass)
    _cache_file(key).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
