#!/usr/bin/env python3
"""Track which source videos were already used in montages."""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

USED_SOURCES_PATH = Path("/root/data/mlbb/used_source_videos.json")
DEFAULT_MAX_AGE_HOURS = float(
    __import__("os").environ.get("SOURCE_MAX_AGE_HOURS", "36")
)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_used() -> dict:
    if not USED_SOURCES_PATH.exists():
        return {"hashes": [], "paths": []}
    try:
        return json.loads(USED_SOURCES_PATH.read_text())
    except Exception:
        return {"hashes": [], "paths": []}


def save_used(data: dict, keep: int = 8000) -> None:
    USED_SOURCES_PATH.parent.mkdir(parents=True, exist_ok=True)
    data["hashes"] = list(data.get("hashes", []))[-keep:]
    data["paths"] = list(data.get("paths", []))[-keep:]
    data["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    USED_SOURCES_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2))


def is_used(path: Path, data: dict | None = None) -> bool:
    data = data or load_used()
    hashes = set(data.get("hashes", []))
    paths = set(data.get("paths", []))
    resolved = str(path.resolve())
    if resolved in paths:
        return True
    try:
        return _file_sha256(path) in hashes
    except OSError:
        return False


def mark_used(paths: list[Path]) -> None:
    data = load_used()
    hashes = set(data.get("hashes", []))
    path_list = set(data.get("paths", []))
    for path in paths:
        try:
            path_list.add(str(path.resolve()))
            hashes.add(_file_sha256(path))
        except OSError:
            continue
    data["hashes"] = sorted(hashes)
    data["paths"] = sorted(path_list)
    save_used(data)


def is_fresh_file(path: Path, max_age_hours: float = DEFAULT_MAX_AGE_HOURS) -> bool:
    if not path.exists():
        return False
    age_sec = time.time() - path.stat().st_mtime
    return age_sec <= max_age_hours * 3600.0


def filter_new_sources(
    paths: list[Path],
    *,
    max_age_hours: float = DEFAULT_MAX_AGE_HOURS,
    skip_used: bool = True,
) -> list[Path]:
    data = load_used() if skip_used else {}
    fresh: list[Path] = []
    for path in paths:
        if not path.exists() or path.suffix.lower() != ".mp4":
            continue
        if not is_fresh_file(path, max_age_hours):
            continue
        if skip_used and is_used(path, data):
            continue
        fresh.append(path)
    return fresh
