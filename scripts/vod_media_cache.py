#!/usr/bin/env python3
"""Content-hash caches for ffprobe, audio windows and extracted frames."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

DEFAULT_ROOT = Path(os.environ.get("VOD_MEDIA_CACHE_DIR", "/root/data/vod_media_cache"))


def cache_root() -> Path:
    root = Path(os.environ.get("VOD_MEDIA_CACHE_DIR", str(DEFAULT_ROOT)))
    root.mkdir(parents=True, exist_ok=True)
    return root


def file_fingerprint(path: Path) -> str:
    """Stable id for a media file (path + size + mtime)."""
    p = Path(path).resolve()
    st = p.stat()
    blob = f"{p}|{st.st_size}|{st.st_mtime_ns}".encode("utf-8", errors="replace")
    return hashlib.sha256(blob).hexdigest()[:40]


def _bucket(kind: str, key: str) -> Path:
    d = cache_root() / kind
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{key}.json"


def get_json(kind: str, key: str) -> dict[str, Any] | None:
    path = _bucket(kind, key)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    max_age = float(os.environ.get("VOD_MEDIA_CACHE_MAX_AGE_SEC", str(14 * 86400)))
    ts = float(payload.get("ts") or 0.0)
    if max_age > 0 and ts and (time.time() - ts) > max_age:
        return None
    data = payload.get("data")
    return data if isinstance(data, dict) else None


def set_json(kind: str, key: str, data: dict[str, Any]) -> Path:
    path = _bucket(kind, key)
    tmp = path.with_suffix(".tmp")
    payload = {"ts": time.time(), "data": data}
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)
    return path


def cached_ffprobe(path: Path, probe_fn: Callable[[Path], dict[str, Any]]) -> dict[str, Any]:
    """Return ffprobe metadata, caching by file fingerprint."""
    p = Path(path)
    key = file_fingerprint(p)
    hit = get_json("ffprobe", key)
    if hit is not None:
        return hit
    data = probe_fn(p) or {}
    if isinstance(data, dict) and data:
        try:
            set_json("ffprobe", key, data)
        except OSError:
            pass
    return data if isinstance(data, dict) else {}


def cached_audio_window(
    path: Path,
    start_sec: float,
    dur_sec: float,
    compute_fn: Callable[[Path, float, float], dict[str, Any]],
) -> dict[str, Any]:
    """Cache audio/PANNs window scores keyed by fingerprint + window."""
    p = Path(path)
    key = f"{file_fingerprint(p)}_{int(start_sec * 10)}_{int(dur_sec * 10)}"
    hit = get_json("audio", key)
    if hit is not None:
        return hit
    data = compute_fn(p, float(start_sec), float(dur_sec)) or {}
    if isinstance(data, dict) and data:
        try:
            set_json("audio", key, data)
        except OSError:
            pass
    return data if isinstance(data, dict) else {}


def extract_frame_cached(
    path: Path,
    at_sec: float,
    *,
    width: int = 320,
) -> Path | None:
    """Extract a JPEG frame once per fingerprint+timestamp."""
    p = Path(path)
    key = f"{file_fingerprint(p)}_{int(max(0.0, at_sec) * 10)}_{width}"
    out = cache_root() / "frames" / f"{key}.jpg"
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists() and out.stat().st_size > 100:
        return out
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        f"{max(0.0, float(at_sec)):.3f}",
        "-i",
        str(p),
        "-frames:v",
        "1",
        "-vf",
        f"scale={int(width)}:-2",
        "-y",
        str(out),
    ]
    try:
        subprocess.run(cmd, check=False, timeout=30, capture_output=True)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.exists() and out.stat().st_size > 100:
        return out
    return None


def audio_preflight_ok(
    path: Path,
    *,
    min_duration_sec: float = 60.0,
    probe_fn: Callable[[Path], dict[str, Any]] | None = None,
) -> tuple[bool, str, dict[str, Any]]:
    """Cheap reject before heavy CV/OCR: duration + audio stream presence."""

    def _probe(p: Path) -> dict[str, Any]:
        if probe_fn is not None:
            return probe_fn(p)
        from smart_video_editor import ffprobe_json

        return ffprobe_json(p)

    meta = cached_ffprobe(path, _probe)
    if not meta:
        return False, "ffprobe_empty", {}
    fmt = meta.get("format") if isinstance(meta.get("format"), dict) else {}
    try:
        duration = float(fmt.get("duration") or 0.0)
    except (TypeError, ValueError):
        duration = 0.0
    if duration < float(min_duration_sec):
        return False, f"too_short:{duration:.1f}", {"duration": duration}
    streams = meta.get("streams") if isinstance(meta.get("streams"), list) else []
    has_audio = any(isinstance(s, dict) and s.get("codec_type") == "audio" for s in streams)
    has_video = any(isinstance(s, dict) and s.get("codec_type") == "video" for s in streams)
    if not has_video:
        return False, "no_video", {"duration": duration}
    if not has_audio:
        return False, "no_audio", {"duration": duration}
    return True, "ok", {"duration": duration, "streams": len(streams)}


def cached_feature(
    kind: str,
    path: Path,
    *,
    start_sec: float = 0.0,
    dur_sec: float = 0.0,
    extra_key: str = "",
    compute_fn: Callable[..., dict[str, Any]],
) -> dict[str, Any]:
    """Generic content-hash cache for OCR / motion / killfeed / other heavy features."""
    p = Path(path)
    key = f"{file_fingerprint(p)}_{int(start_sec * 10)}_{int(dur_sec * 10)}"
    if extra_key:
        key = f"{key}_{extra_key}"
    hit = get_json(kind, key)
    if hit is not None:
        return hit
    data = compute_fn(p, float(start_sec), float(dur_sec)) or {}
    if isinstance(data, dict) and data:
        try:
            set_json(kind, key, data)
        except OSError:
            pass
    return data if isinstance(data, dict) else {}


def cached_ocr_window(
    path: Path,
    start_sec: float,
    dur_sec: float,
    compute_fn: Callable[[Path, float, float], dict[str, Any]],
) -> dict[str, Any]:
    return cached_feature("ocr", path, start_sec=start_sec, dur_sec=dur_sec, compute_fn=compute_fn)


def cached_motion_window(
    path: Path,
    start_sec: float,
    dur_sec: float,
    compute_fn: Callable[[Path, float, float], dict[str, Any]],
) -> dict[str, Any]:
    return cached_feature("motion", path, start_sec=start_sec, dur_sec=dur_sec, compute_fn=compute_fn)
