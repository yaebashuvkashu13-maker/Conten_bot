#!/usr/bin/env python3
"""One-shot exemplary MLBB montage from hero dataset + fresh TikTok gameplay."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
from pathlib import Path

from source_freshness import filter_new_sources, is_used

HERO_ROOT = Path("/root/hero_datasets")
TIKTOK_ROOT = Path("/root/datasets/tiktok/mlbb")
OUTPUT_DIR = Path("/root/videos")
STATE_PATH = Path("/root/data/mlbb/last_etalon_montage.json")
MIN_SOURCES = int(os.environ.get("ETALON_MIN_SOURCES", "4"))
MAX_SOURCES = int(os.environ.get("ETALON_MAX_SOURCES", "8"))
TIKTOK_SCAN = int(os.environ.get("ETALON_TIKTOK_SCAN", "30"))


def gather_hero_paths(limit: int) -> list[Path]:
    """Curated hero clips — prefer unused files from different heroes."""
    per_hero: dict[str, list[Path]] = {}
    if not HERO_ROOT.exists():
        return []
    for hero_dir in sorted(HERO_ROOT.iterdir()):
        if not hero_dir.is_dir():
            continue
        files = sorted(hero_dir.glob("*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True)
        fresh = [p for p in files if not is_used(p)]
        if fresh:
            per_hero[hero_dir.name] = fresh
    picked: list[Path] = []
    while len(picked) < limit and per_hero:
        for name in sorted(per_hero.keys()):
            bucket = per_hero.get(name) or []
            if not bucket:
                continue
            picked.append(bucket.pop(0))
            if not bucket:
                per_hero.pop(name, None)
            if len(picked) >= limit:
                break
    return picked


def gather_tiktok_paths(limit: int) -> list[Path]:
    if not TIKTOK_ROOT.exists() or limit <= 0:
        return []
    paths = [
        p
        for p in sorted(TIKTOK_ROOT.rglob("*.mp4"), key=lambda item: item.stat().st_mtime, reverse=True)
        if "non_gameplay" not in p.parts
    ]
    fresh = filter_new_sources(paths, max_age_hours=float(os.environ.get("SOURCE_MAX_AGE_HOURS", "72")))
    return fresh[:limit]


def build_queue(paths: list[Path], chat_id: str) -> Path:
    fd, queue_path = tempfile.mkstemp(prefix="etalon-mlbb-", suffix=".txt", dir="/tmp")
    with os.fdopen(fd, "w") as handle:
        for path in paths:
            hero = path.parent.name if path.parent.parent == HERO_ROOT else "MLBB"
            handle.write(f"{path}|MLBB etalon {hero}|{chat_id}\n")
    return Path(queue_path)


def main() -> int:
    chat_id = os.environ.get("TG_CHAT_ID", "")
    if not chat_id:
        print("[etalon] TG_CHAT_ID missing")
        return 1

    hero_count = min(MAX_SOURCES, max(MIN_SOURCES, int(os.environ.get("ETALON_HERO_COUNT", "6"))))
    picked = gather_hero_paths(hero_count)
    tiktok_extra = max(0, MAX_SOURCES - len(picked))
    if tiktok_extra:
        picked.extend(gather_tiktok_paths(min(tiktok_extra, TIKTOK_SCAN)))
    # Deduplicate while preserving order
    seen: set[str] = set()
    unique: list[Path] = []
    for path in picked:
        key = str(path.resolve())
        if key in seen:
            continue
        seen.add(key)
        unique.append(path)
    picked = unique[:MAX_SOURCES]

    if len(picked) < MIN_SOURCES:
        print(f"[etalon] only {len(picked)} sources (need {MIN_SOURCES})")
        return 1
    print(f"[etalon] queue {len(picked)} sources ({sum(1 for p in picked if HERO_ROOT in p.parents)} hero)")

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
            "STRICT_GAMEPLAY": "0",
            "TARGET_DURATION": "45",
            "MIN_FINAL_DURATION": "33",
            "MAX_FINAL_DURATION": "57",
            "MIN_HIGHLIGHTS": "3",
            "MAX_HIGHLIGHTS": "4",
            "SMART_ADD_MUSIC": "0",
            "SMART_GAME_AUDIO_ONLY": "1",
            "BLUR_NICKNAME": "0",
            "SMART_MIN_HUD": "16.5",
            "SMART_MIN_HUD_FRAME_RATE": "0.62",
            "SMART_MIN_CENTER_MOTION": "0.017",
            "SMART_MAX_CHAT_PANEL": "0.15",
            "SMART_MAX_CENTER_TEXT": "0.11",
            "SMART_MIN_BIN_MOTION": "0.013",
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
        print(f"[etalon] done rc={result.returncode}")
        return result.returncode
    finally:
        queue_path.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
