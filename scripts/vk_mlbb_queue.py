#!/usr/bin/env python3
"""Queue for owner-uploaded MLBB clips → scheduled VK publishing."""

from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

QUEUE_ROOT = Path("/root/data/mlbb/vk_mlbb_queue")
PENDING_DIR = QUEUE_ROOT / "pending"
PUBLISHED_DIR = QUEUE_ROOT / "published"
PREPARED_DIR = QUEUE_ROOT / "prepared"
EXEMPLAR_DIR = Path("/root/content_bot_ml/data/highlight_exemplars/mobile_legends/good")
LOG_FILE = QUEUE_ROOT / "publish.log"
INGEST_LOG = QUEUE_ROOT / "ingest.jsonl"

BATCH_SIZE = int(__import__("os").environ.get("VK_MLBB_BATCH_SIZE", "3"))


def ensure_dirs() -> None:
    for path in (QUEUE_ROOT, PENDING_DIR, PUBLISHED_DIR, PREPARED_DIR):
        path.mkdir(parents=True, exist_ok=True)
    EXEMPLAR_DIR.mkdir(parents=True, exist_ok=True)


def pending_items() -> list[Path]:
    ensure_dirs()
    files = [p for p in PENDING_DIR.glob("*.mp4") if p.is_file()]
    return sorted(files, key=lambda p: p.stat().st_mtime)


def pending_count() -> int:
    return len(pending_items())


def enqueue_video(source: Path, *, chat_id: str = "", label: str = "") -> Path:
    ensure_dirs()
    stamp = time.strftime("%Y%m%d_%H%M%S")
    dest = PENDING_DIR / f"{stamp}_{source.stem[:40]}.mp4"
    shutil.copy2(source, dest)
    meta = {
        "path": str(dest),
        "chat_id": chat_id,
        "label": label,
        "added_at": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
    }
    dest.with_suffix(".json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    _log_ingest(dest, meta)
    _mirror_exemplar(dest)
    return dest


def _log_ingest(path: Path, meta: dict) -> None:
    row = {"event": "enqueue", "path": str(path), **meta}
    with INGEST_LOG.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def _mirror_exemplar(path: Path) -> None:
    """Keep a copy for MLBB exemplar learning (owner-curated VK uploads)."""
    try:
        stamp = path.stem
        dest = EXEMPLAR_DIR / f"vk_owner_{stamp}.mp4"
        if not dest.exists():
            shutil.copy2(path, dest)
    except OSError:
        pass


def pop_batch(limit: int = BATCH_SIZE) -> list[Path]:
    items = pending_items()
    return items[:limit]


def archive_published(path: Path, *, vk_video_id: str = "", slot: str = "") -> Path:
    ensure_dirs()
    day = time.strftime("%Y%m%d")
    out_dir = PUBLISHED_DIR / day
    out_dir.mkdir(parents=True, exist_ok=True)
    dest = out_dir / path.name
    shutil.move(str(path), str(dest))
    meta_path = path.with_suffix(".json")
    meta = {}
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            meta = {}
        shutil.move(str(meta_path), str(dest.with_suffix(".json")))
    meta.update(
        {
            "published_at": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
            "vk_video_id": vk_video_id,
            "slot": slot,
        }
    )
    dest.with_suffix(".json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return dest


def append_publish_log(line: str) -> None:
    ensure_dirs()
    with LOG_FILE.open("a", encoding="utf-8") as handle:
        handle.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {line}\n")
