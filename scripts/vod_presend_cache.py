#!/usr/bin/env python3
"""Disk cache for expensive presend / peak feature reports."""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any

CACHE_VERSION = 1
DEFAULT_ROOT = "/root/data/pubg/presend_cache"


def cache_enabled() -> bool:
    return os.environ.get("VOD_PRESEND_CACHE", "1") == "1"


def cache_root() -> Path:
    return Path(os.environ.get("VOD_PRESEND_CACHE_DIR", DEFAULT_ROOT))


def cache_ttl_sec() -> int:
    return max(1800, int(os.environ.get("VOD_PRESEND_CACHE_TTL_SEC", str(6 * 3600))))


def _window_key(video_path: Path, start_sec: float, duration_sec: float) -> str:
    p = video_path.resolve()
    st = p.stat()
    blob = (
        f"v{CACHE_VERSION}|{p}|{st.st_mtime_ns}|{st.st_size}|"
        f"{round(float(start_sec), 2)}|{round(float(duration_sec), 2)}"
    )
    return hashlib.sha256(blob.encode("utf-8", errors="replace")).hexdigest()[:32]


def get_presend(
    video_path: Path,
    start_sec: float,
    duration_sec: float,
) -> tuple[bool, str, dict[str, Any]] | None:
    if not cache_enabled() or not video_path.is_file():
        return None
    path = cache_root() / f"{_window_key(video_path, start_sec, duration_sec)}.json"
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    saved_at = float(payload.get("saved_at") or 0)
    if saved_at <= 0 or (time.time() - saved_at) > cache_ttl_sec():
        return None
    return bool(payload.get("ok")), str(payload.get("reason") or ""), dict(payload.get("report") or {})


def put_presend(
    video_path: Path,
    start_sec: float,
    duration_sec: float,
    ok: bool,
    reason: str,
    report: dict[str, Any],
) -> None:
    if not cache_enabled() or not video_path.is_file():
        return
    cache_root().mkdir(parents=True, exist_ok=True)
    key = _window_key(video_path, start_sec, duration_sec)
    payload = {
        "version": CACHE_VERSION,
        "saved_at": time.time(),
        "ok": bool(ok),
        "reason": str(reason)[:200],
        "report": report,
    }
    path = cache_root() / f"{key}.json"
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def clear_presend_cache(video_path: Path | None = None) -> int:
    """Drop cached presend results (all or one VOD's windows)."""
    root = cache_root()
    if not root.is_dir():
        return 0
    removed = 0
    if video_path is None:
        for path in root.glob("*.json"):
            try:
                path.unlink()
                removed += 1
            except OSError:
                pass
        return removed
    target = video_path.resolve()
    for path in root.glob("*.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        report = payload.get("report") or {}
        if str(report.get("video") or "") == str(target):
            try:
                path.unlink()
                removed += 1
            except OSError:
                pass
    return removed


__all__ = ["get_presend", "put_presend", "cache_enabled", "clear_presend_cache"]
