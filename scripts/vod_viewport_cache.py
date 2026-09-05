#!/usr/bin/env python3
"""Per-VOD viewport crop cache — detect once, reuse across OCR/visual gates."""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any

CACHE_VERSION = 1
DEFAULT_ROOT = "/root/data/pubg/viewport_cache"


def cache_enabled() -> bool:
    return os.environ.get("VOD_VIEWPORT_CACHE", "1") == "1"


def cache_root() -> Path:
    return Path(os.environ.get("VOD_VIEWPORT_CACHE_DIR", DEFAULT_ROOT))


def cache_ttl_sec() -> int:
    return max(3600, int(os.environ.get("VOD_VIEWPORT_CACHE_TTL_SEC", str(7 * 24 * 3600))))


def _vod_key(path: Path) -> str:
    p = path.resolve()
    st = p.stat()
    blob = f"v{CACHE_VERSION}|{p}|{st.st_mtime_ns}|{st.st_size}"
    return hashlib.sha256(blob.encode("utf-8", errors="replace")).hexdigest()[:32]


def _cache_file(key: str) -> Path:
    return cache_root() / f"{key}.json"


def get_cached_viewport(path: Path) -> dict[str, Any] | None:
    if not cache_enabled() or not path.is_file():
        return None
    cache_file = _cache_file(_vod_key(path))
    if not cache_file.is_file():
        return None
    try:
        payload = json.loads(cache_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    saved_at = float(payload.get("saved_at") or 0)
    if saved_at <= 0 or (time.time() - saved_at) > cache_ttl_sec():
        return None
    return payload


def put_cached_viewport(path: Path, payload: dict[str, Any]) -> None:
    if not cache_enabled() or not path.is_file():
        return
    cache_root().mkdir(parents=True, exist_ok=True)
    payload = dict(payload)
    payload["version"] = CACHE_VERSION
    payload["saved_at"] = time.time()
    payload["vod"] = str(path.resolve())
    cache_file = _cache_file(_vod_key(path))
    tmp = cache_file.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, cache_file)


def _iou(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> float:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    x1 = max(ax, bx)
    y1 = max(ay, by)
    x2 = min(ax + aw, bx + bw)
    y2 = min(ay + ah, by + bh)
    if x2 <= x1 or y2 <= y1:
        return 0.0
    inter = (x2 - x1) * (y2 - y1)
    union = aw * ah + bw * bh - inter
    return inter / max(union, 1)


def detect_viewport_cached(
    video_path: Path,
    start_sec: float,
    duration_sec: float,
) -> tuple[int, int, int, int] | None:
    """Stable per-VOD viewport; probes start/mid/end when cache miss."""
    from gameplay_gate import detect_game_viewport_crop
    from smart_video_editor import ffprobe_duration

    hit = get_cached_viewport(video_path)
    if hit and hit.get("crop"):
        crop = tuple(int(v) for v in hit["crop"])
        if len(crop) == 4:
            return crop  # type: ignore[return-value]

    dur = ffprobe_duration(video_path)
    if dur <= 0:
        return detect_game_viewport_crop(video_path, start_sec, duration_sec)

    probes = sorted(
        {
            max(0.0, start_sec),
            max(0.0, min(dur - 1.0, dur * 0.5)),
            max(0.0, min(dur - 1.0, start_sec + duration_sec * 0.5)),
        }
    )
    crops: list[tuple[int, int, int, int]] = []
    for probe in probes:
        crop = detect_game_viewport_crop(video_path, probe, min(8.0, max(2.0, duration_sec)))
        if crop is not None:
            crops.append(crop)
    if not crops:
        return None

    stable = crops[0]
    min_iou = float(os.environ.get("VOD_VIEWPORT_MIN_IOU", "0.90"))
    ious = [_iou(stable, other) for other in crops[1:]]
    stable_enough = not ious or min(ious) >= min_iou
    put_cached_viewport(
        video_path,
        {
            "crop": list(stable),
            "probes": [round(p, 1) for p in probes],
            "stable": stable_enough,
            "ious": [round(v, 3) for v in ious],
        },
    )
    return stable


__all__ = ["detect_viewport_cached", "get_cached_viewport", "put_cached_viewport"]
