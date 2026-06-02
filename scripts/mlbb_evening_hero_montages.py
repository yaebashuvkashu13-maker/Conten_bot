#!/usr/bin/env python3
"""Build 1-2 single-hero MLBB montages (game audio only) for evening publish."""

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
STATE_PATH = Path("/root/data/mlbb/evening_montage_state.json")
SMART_EDITOR = Path("/usr/local/bin/smart_video_editor.py")
HEROES_JSON = Path("/root/content_bot_ml/config/mlbb_heroes.json")
if not HEROES_JSON.exists():
    HEROES_JSON = Path(__file__).resolve().parent.parent / "config/mlbb_heroes.json"


def hero_title(hero_id: str) -> str:
    return hero_id.replace("_", " ").title()


def heroes_by_count(min_clips: int = 8) -> list[tuple[str, int]]:
    rows: list[tuple[str, int]] = []
    if not HERO_ROOT.exists():
        return rows
    for folder in HERO_ROOT.iterdir():
        if not folder.is_dir():
            continue
        count = len(list(folder.glob("*.mp4")))
        if count >= min_clips:
            rows.append((folder.name, count))
    rows.sort(key=lambda x: -x[1])
    return rows


def build_queue(paths: list[Path], chat_id: str, label: str) -> Path:
    fd, queue_path = tempfile.mkstemp(prefix="hero-evening-", suffix=".txt", dir="/tmp")
    with os.fdopen(fd, "w") as handle:
        for p in paths:
            handle.write(f"{p}|{label}|{chat_id}\n")
    return Path(queue_path)


def run_one(hero_id: str, chat_id: str, theme: str) -> int:
    folder = HERO_ROOT / hero_id
    mp4s = sorted(folder.glob("*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True)
    if len(mp4s) < 4:
        print(f"skip {hero_id}: only {len(mp4s)} clips")
        return 3

    random.shuffle(mp4s)
    sources = mp4s[: min(12, len(mp4s))]
    label = f"{theme} | {hero_title(hero_id)}"
    queue = build_queue(sources, chat_id, label)

    env = os.environ.copy()
    env.update(
        {
            "QUEUE_FILE": str(queue),
            "OUTPUT_DIR": str(OUTPUT_DIR),
            "SEND_TELEGRAM": "1",
            "MAX_SOURCES": str(len(sources)),
            "MIN_HIGHLIGHTS": "3",
            "MAX_HIGHLIGHTS": "4",
            "SMART_ADD_MUSIC": "0",
            "BLUR_NICKNAME": "0",
            "STRICT_GAMEPLAY": "1",
            "SMART_MIN_HUD": os.environ.get("SMART_MIN_HUD", "16"),
            "SMART_MAX_OVERLAY_TEXT": os.environ.get("SMART_MAX_OVERLAY_TEXT", "0.28"),
            "SMART_MAX_REJECT_SIM": os.environ.get("SMART_MAX_REJECT_SIM", "0.76"),
            "SMART_MIN_HUD_FRAME_RATE": os.environ.get("SMART_MIN_HUD_FRAME_RATE", "0.60"),
            "OUTPUT_BASENAME": f"mlbb_{hero_id}_{theme.replace(' ', '_').lower()[:24]}",
        }
    )
    try:
        return subprocess.run(["python3", str(SMART_EDITOR)], env=env, check=False).returncode
    finally:
        queue.unlink(missing_ok=True)


def main() -> int:
    chat_id = os.environ.get("TG_CHAT_ID", "")
    if not chat_id:
        print("TG_CHAT_ID missing")
        return 1

    count = int(os.environ.get("MLBB_EVENING_COUNT", "2"))
    min_clips = int(os.environ.get("MLBB_EVENING_MIN_CLIPS", "8"))
    theme = os.environ.get("MLBB_EVENING_THEME", "Hero Highlights")
    only_heroes = [
        h.strip().lower().replace(" ", "_")
        for h in os.environ.get("MLBB_EVENING_HEROES", "").split(",")
        if h.strip()
    ]

    ranked = heroes_by_count(min_clips=min_clips)
    if not ranked:
        ranked = heroes_by_count(min_clips=4)
    if not ranked:
        print("no hero folders with enough clips")
        return 2

    state = {}
    if STATE_PATH.exists():
        try:
            state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except Exception:
            state = {}

    day_key = time.strftime("%Y-%m-%d")
    done_today = set(state.get(day_key, {}).get("heroes", []))
    results = []

    for hero_id, clip_count in ranked:
        if only_heroes and hero_id not in only_heroes:
            continue
        if len(results) >= count:
            break
        if hero_id in done_today:
            continue
        print(f"montage hero={hero_id} clips={clip_count}")
        rc = run_one(hero_id, chat_id, theme)
        results.append({"hero": hero_id, "rc": rc, "clips": clip_count})
        if rc == 0:
            done_today.add(hero_id)

    state.setdefault(day_key, {})["heroes"] = sorted(done_today)
    state[day_key]["last_runs"] = results
    state[day_key]["at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

    ok = sum(1 for r in results if r["rc"] == 0)
    print(json.dumps({"ok": ok, "total": len(results), "results": results}, ensure_ascii=False))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
