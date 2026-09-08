#!/usr/bin/env python3
"""Track YouTube channel/download health and temporarily block bad sources."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

DEFAULT_PATH = "/root/data/pubg/youtube_source_health.json"
BLOCK_SEC = 6 * 3600
MIN_SCORE = 0.30


def health_path() -> Path:
    return Path(os.environ.get("YOUTUBE_SOURCE_HEALTH_PATH", DEFAULT_PATH))


def _read() -> dict[str, Any]:
    path = health_path()
    if not path.is_file():
        return {"channels": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"channels": {}}
    if not isinstance(data, dict):
        return {"channels": {}}
    data.setdefault("channels", {})
    return data


def _write(data: dict[str, Any]) -> None:
    path = health_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def channel_key(channel_id: str = "", channel_name: str = "", url: str = "") -> str:
    for value in (channel_id.strip(), channel_name.strip().lower(), url.strip()):
        if value:
            return value[:120]
    return "unknown"


def record_download_result(
    *,
    channel_id: str = "",
    channel_name: str = "",
    url: str = "",
    ok: bool,
    error_kind: str = "",
) -> None:
    key = channel_key(channel_id, channel_name, url)
    data = _read()
    row = dict(data["channels"].get(key) or {})
    attempts = int(row.get("attempts") or 0) + 1
    successes = int(row.get("successes") or 0) + (1 if ok else 0)
    failures = int(row.get("failures") or 0) + (0 if ok else 1)
    score = successes / max(attempts, 1)
    row.update(
        {
            "attempts": attempts,
            "successes": successes,
            "failures": failures,
            "score": round(score, 4),
            "last_ok": ok,
            "last_error_kind": error_kind[:80] if not ok else "",
            "last_at": time.time(),
        }
    )
    if score < float(os.environ.get("YOUTUBE_SOURCE_MIN_SCORE", str(MIN_SCORE))):
        row["blocked_until"] = time.time() + int(
            os.environ.get("YOUTUBE_SOURCE_BLOCK_SEC", str(BLOCK_SEC))
        )
    else:
        row.pop("blocked_until", None)
    data["channels"][key] = row
    _write(data)


def is_blocked(channel_id: str = "", channel_name: str = "", url: str = "") -> tuple[bool, str]:
    key = channel_key(channel_id, channel_name, url)
    row = _read()["channels"].get(key) or {}
    blocked_until = float(row.get("blocked_until") or 0)
    if blocked_until > time.time():
        return True, f"blocked_until={int(blocked_until)} score={row.get('score', 0)}"
    return False, ""


def classify_download_error(stderr: str) -> str:
    text = (stderr or "").lower()
    if "sign in" in text or "cookies" in text or "bot" in text:
        return "auth"
    if "unavailable" in text or "private" in text or "removed" in text:
        return "unavailable"
    if "format" in text or "fragment" in text:
        return "quality"
    if "timeout" in text or "timed out" in text:
        return "timeout"
    return "download"


__all__ = [
    "classify_download_error",
    "is_blocked",
    "record_download_result",
]
