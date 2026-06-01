#!/usr/bin/env python3
"""Build montages only from NEW sources (recent TikTok + fresh Telegram uploads)."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
from pathlib import Path

from source_freshness import filter_new_sources, mark_used

TIKTOK_ROOT = Path("/root/datasets/tiktok/mlbb")
TELEGRAM_PENDING = Path("/root/telegram_uploads/pending")
OUTPUT_DIR = Path("/root/videos")
STATE_PATH = Path("/root/data/mlbb/last_new_sources_cycle.json")
MAX_AGE_HOURS = float(os.environ.get("SOURCE_MAX_AGE_HOURS", "36"))
MAX_SOURCES = int(os.environ.get("NEW_SOURCE_MAX_PER_CYCLE", "12"))


def gather_candidate_paths() -> list[Path]:
    paths: list[Path] = []
    if TIKTOK_ROOT.exists():
        paths.extend(sorted(TIKTOK_ROOT.rglob("*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True))
    if TELEGRAM_PENDING.exists():
        for chat_dir in TELEGRAM_PENDING.iterdir():
            if not chat_dir.is_dir():
                continue
            paths.extend(chat_dir.glob("*.mp4"))
    return paths


def build_queue(paths: list[Path], chat_id: str) -> Path:
    fd, queue_path = tempfile.mkstemp(prefix="new-sources-", suffix=".txt", dir="/tmp")
    with os.fdopen(fd, "w") as handle:
        for path in paths:
            handle.write(f"{path}|Hayabusa|{chat_id}\n")
    return Path(queue_path)


def main() -> int:
    chat_id = os.environ.get("TG_CHAT_ID", "")
    if not chat_id:
        print("[new-sources] TG_CHAT_ID missing")
        return 1

    all_paths = gather_candidate_paths()
    new_paths = filter_new_sources(all_paths, max_age_hours=MAX_AGE_HOURS)[:MAX_SOURCES]
    if not new_paths:
        print("[new-sources] no fresh unused videos, skip montage")
        STATE_PATH.write_text(
            json.dumps(
                {
                    "skipped": True,
                    "reason": "no_new_sources",
                    "at": time.strftime("%Y-%m-%d %H:%M:%S"),
                },
                indent=2,
            )
        )
        return 0

    queue_path = build_queue(new_paths, chat_id)
    env = os.environ.copy()
    env.update(
        {
            "QUEUE_FILE": str(queue_path),
            "MAX_SOURCES": str(len(new_paths)),
            "SEND_TELEGRAM": "1",
            "OUTPUT_DIR": str(OUTPUT_DIR),
        }
    )
    try:
        result = subprocess.run(["/usr/local/bin/smart_video_editor.py"], env=env, check=False)
        if result.returncode == 0:
            mark_used(new_paths)
        STATE_PATH.write_text(
            json.dumps(
                {
                    "skipped": False,
                    "sources": [str(p) for p in new_paths],
                    "count": len(new_paths),
                    "returncode": result.returncode,
                    "at": time.strftime("%Y-%m-%d %H:%M:%S"),
                },
                indent=2,
            )
        )
        print(f"[new-sources] processed {len(new_paths)} files, rc={result.returncode}")
        return result.returncode
    finally:
        queue_path.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
