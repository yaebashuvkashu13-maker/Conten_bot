#!/usr/bin/env python3
"""
Generate MLBB montages from a SINGLE hero using existing hero-datasets.

Outputs a queue for smart_video_editor.py and sets a neutral caption theme:
- "Top 3 Savage of last day"
- "Top 3 Maniac of last day"

This avoids drawing attention to different nicknames across clips.
"""

from __future__ import annotations

import json
import os
import random
import subprocess
import tempfile
import time
from pathlib import Path

HERO_ROOT = Path("/root/hero_datasets")
OUTPUT_DIR = Path("/root/videos")
STATE_PATH = Path("/root/data/mlbb/daily_top_state.json")
SMART_EDITOR = Path("/usr/local/bin/smart_video_editor.py")


def load_state() -> dict:
    if not STATE_PATH.exists():
        return {}
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def pick_hero(prefer: list[str] | None = None) -> str | None:
    if not HERO_ROOT.exists():
        return None
    heroes = [p.name for p in HERO_ROOT.iterdir() if p.is_dir()]
    if not heroes:
        return None
    if prefer:
        for h in prefer:
            if h in heroes:
                return h
    return random.choice(heroes)


def pick_sources(hero_id: str, limit: int = 10) -> list[Path]:
    folder = HERO_ROOT / hero_id
    if not folder.exists():
        return []
    mp4s = sorted(folder.glob("*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True)
    return mp4s[:limit]


def build_queue(paths: list[Path], chat_id: str, label: str) -> Path:
    fd, queue_path = tempfile.mkstemp(prefix="single-hero-", suffix=".txt", dir="/tmp")
    with os.fdopen(fd, "w") as handle:
        for p in paths:
            handle.write(f"{p}|{label}|{chat_id}\n")
    return Path(queue_path)


def run(theme: str) -> int:
    chat_id = os.environ.get("TG_CHAT_ID", "")
    if not chat_id:
        print("TG_CHAT_ID missing")
        return 1
    prefer_hero = [h.strip() for h in os.environ.get("MLBB_DAILY_HERO", "").split(",") if h.strip()]
    hero = pick_hero(prefer_hero)
    if not hero:
        print("no hero datasets found")
        return 2

    state = load_state()
    key = f"{time.strftime('%Y-%m-%d')}:{theme}:{hero}"
    if state.get("last_key") == key and state.get("last_ok"):
        print("already generated today:", key)
        return 0

    sources = pick_sources(hero, limit=int(os.environ.get("MLBB_DAILY_SOURCES", "14")))
    if len(sources) < 4:
        print("not enough sources for hero", hero, "count", len(sources))
        return 3

    random.shuffle(sources)
    sources = sources[: min(len(sources), 12)]
    # Keep hero out of the public label to avoid mismatches and reduce attention.
    label = theme
    queue = build_queue(sources, chat_id, label)

    env = os.environ.copy()
    env.update(
        {
            "QUEUE_FILE": str(queue),
            "OUTPUT_DIR": str(OUTPUT_DIR),
            "SEND_TELEGRAM": "1",
            "MAX_SOURCES": str(len(sources)),
            "MIN_HIGHLIGHTS": "3",
            "MAX_HIGHLIGHTS": "3",
            "SINGLE_HERO_MODE": "1",
            "SINGLE_HERO_ID": hero,
            "SMART_ADD_MUSIC": "0",
            "SMART_GAME_AUDIO_ONLY": "1",
            "SMART_STRIP_MUSIC_BED": "1",
            "SMART_REJECT_TRAINING": "1",
            "SMART_REJECT_MUSIC_BED": "1",
        }
    )
    try:
        rc = subprocess.run(["python3", str(SMART_EDITOR)], env=env, check=False).returncode
        state.update({"last_key": key, "last_ok": rc == 0, "hero": hero, "theme": theme, "at": time.time()})
        save_state(state)
        return rc
    finally:
        queue.unlink(missing_ok=True)


def main() -> int:
    mode = (os.environ.get("MLBB_DAILY_MODE") or "savage").lower()
    if mode not in {"savage", "maniac"}:
        mode = "savage"
    theme = "Top 3 Savage of last day" if mode == "savage" else "Top 3 Maniac of last day"
    return run(theme)


if __name__ == "__main__":
    raise SystemExit(main())

