#!/usr/bin/env python3
"""Track which source videos were already used in montages."""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

USED_SOURCES_PATH = Path("/root/data/mlbb/used_source_videos.json")
USED_BY_CHAT_PATH = Path("/root/data/mlbb/used_source_by_chat.json")
DEFAULT_MAX_AGE_HOURS = float(
    __import__("os").environ.get("SOURCE_MAX_AGE_HOURS", "36")
)


def _per_chat_tracking_enabled(chat_id: str | None) -> bool:
    if not chat_id:
        return False
    raw = __import__("os").environ.get("SOURCE_PER_CHAT_IDS", "")
    if not raw.strip():
        return False
    allowed = {item.strip() for item in raw.split(",") if item.strip()}
    return str(chat_id) in allowed


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


def load_used_by_chat() -> dict:
    if not USED_BY_CHAT_PATH.exists():
        return {}
    try:
        return json.loads(USED_BY_CHAT_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_used_by_chat(data: dict) -> None:
    USED_BY_CHAT_PATH.parent.mkdir(parents=True, exist_ok=True)
    data["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    USED_BY_CHAT_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def is_used_for_chat(path: Path, chat_id: str) -> bool:
    data = load_used_by_chat()
    entry = data.get(str(chat_id), {})
    resolved = str(path.resolve())
    if resolved in set(entry.get("paths", [])):
        return True
    try:
        return _file_sha256(path) in set(entry.get("hashes", []))
    except OSError:
        return False


def mark_used_for_chat(paths: list[Path], chat_id: str) -> None:
    data = load_used_by_chat()
    entry = data.setdefault(str(chat_id), {"hashes": [], "paths": []})
    hashes = set(entry.get("hashes", []))
    path_list = set(entry.get("paths", []))
    for path in paths:
        try:
            path_list.add(str(path.resolve()))
            hashes.add(_file_sha256(path))
        except OSError:
            continue
    entry["hashes"] = sorted(hashes)[-2000:]
    entry["paths"] = sorted(path_list)[-2000:]
    save_used_by_chat(data)


def mark_used(paths: list[Path], chat_id: str | None = None) -> None:
    if chat_id and _per_chat_tracking_enabled(chat_id):
        mark_used_for_chat(paths, chat_id)
        return
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
    chat_id: str | None = None,
) -> list[Path]:
    global_data = load_used() if skip_used else {}
    per_chat = _per_chat_tracking_enabled(chat_id)
    fresh: list[Path] = []
    for path in paths:
        if not path.exists() or path.suffix.lower() != ".mp4":
            continue
        if not is_fresh_file(path, max_age_hours):
            continue
        if skip_used:
            if per_chat and chat_id and is_used_for_chat(path, chat_id):
                continue
            if not per_chat and is_used(path, global_data):
                continue
        fresh.append(path)
    return fresh


def prune_used_from_queue_file(queue_path: Path, chat_id: str | None = None) -> int:
    """Drop lines that are already used / stale so AUTO_MAKE does not retry them."""
    if not queue_path.exists():
        return 0
    lines = [line for line in queue_path.read_text().splitlines() if line.strip()]
    kept: list[str] = []
    removed = 0
    for line in lines:
        path = Path(line.split("|", 1)[0])
        if path in filter_new_sources([path], chat_id=chat_id):
            kept.append(line)
        else:
            removed += 1
    queue_path.write_text("\n".join(kept) + ("\n" if kept else ""))
    return removed
