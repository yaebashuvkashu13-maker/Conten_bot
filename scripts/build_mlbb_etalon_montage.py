#!/usr/bin/env python3
"""One-shot exemplary MLBB montage from fresh unused gameplay sources."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
from pathlib import Path

from source_freshness import filter_new_sources

TIKTOK_ROOT = Path("/root/datasets/tiktok/mlbb")
OUTPUT_DIR = Path("/root/videos")
STATE_PATH = Path("/root/data/mlbb/last_etalon_montage.json")
MAX_AGE_HOURS = float(os.environ.get("SOURCE_MAX_AGE_HOURS", "72"))
MIN_SOURCES = int(os.environ.get("ETALON_MIN_SOURCES", "4"))
MAX_SOURCES = int(os.environ.get("ETALON_MAX_SOURCES", "8"))


def gather_paths() -> list[Path]:
    paths: list[Path] = []
    if TIKTOK_ROOT.exists():
        paths.extend(
            sorted(TIKTOK_ROOT.rglob("*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True)
        )
    return paths


def build_queue(paths: list[Path], chat_id: str) -> Path:
    fd, queue_path = tempfile.mkstemp(prefix="etalon-mlbb-", suffix=".txt", dir="/tmp")
    with os.fdopen(fd, "w") as handle:
        for path in paths:
            handle.write(f"{path}|MLBB etalon|{chat_id}\n")
    return Path(queue_path)


def main() -> int:
    chat_id = os.environ.get("TG_CHAT_ID", "")
    if not chat_id:
        print("[etalon] TG_CHAT_ID missing")
        return 1

    all_paths = gather_paths()
    fresh = filter_new_sources(all_paths, max_age_hours=MAX_AGE_HOURS)
    if len(fresh) < MIN_SOURCES:
        print(f"[etalon] only {len(fresh)} fresh sources, need {MIN_SOURCES}")
        return 1
    picked = fresh[:MAX_SOURCES]
    queue_path = build_queue(picked, chat_id)

    env = os.environ.copy()
    env.update(
        {
            "QUEUE_FILE": str(queue_path),
            "MAX_SOURCES": str(len(picked)),
            "OUTPUT_DIR": str(OUTPUT_DIR),
            "OUTPUT_BASENAME": "mlbb_etalon",
            "ETALON_MONTAGE": "1",
            "SEND_TELEGRAM": "1",
            "STRICT_GAMEPLAY": "1",
            "TARGET_DURATION": "45",
            "MIN_FINAL_DURATION": "33",
            "MAX_FINAL_DURATION": "57",
            "MIN_HIGHLIGHTS": "3",
            "MAX_HIGHLIGHTS": "4",
            "SMART_ADD_MUSIC": "0",
            "SMART_GAME_AUDIO_ONLY": "1",
            "BLUR_NICKNAME": "0",
            "SMART_MIN_HUD": "17.5",
            "SMART_MIN_HUD_FRAME_RATE": "0.68",
            "SMART_MIN_CENTER_MOTION": "0.019",
            "SMART_MAX_CHAT_PANEL": "0.14",
            "SMART_MAX_CENTER_TEXT": "0.10",
            "SMART_MIN_BIN_MOTION": "0.015",
            "SELECTION_VARIANT": str(int(time.time()) % 5),
        }
    )
    try:
        result = subprocess.run(["/usr/local/bin/smart_video_editor.py"], env=env, check=False)
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(
            json.dumps(
                {
                    "sources": [str(p) for p in picked],
                    "returncode": result.returncode,
                    "at": time.strftime("%Y-%m-%d %H:%M:%S"),
                },
                indent=2,
            )
        )
        print(f"[etalon] done rc={result.returncode} sources={len(picked)}")
        return result.returncode
    finally:
        queue_path.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
