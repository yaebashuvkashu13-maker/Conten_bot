#!/usr/bin/env python3
"""Exemplary single-hero MLBB montage (combat only, game SFX, black bars)."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
from pathlib import Path

from gameplay_gate import (
    path_blocked_by_calibration,
    path_whitelisted_by_calibration,
    profile_looks_like_mlbb_edit,
    source_has_valid_gameplay_window,
)
from source_freshness import is_used

HERO_ROOT = Path("/root/hero_datasets")
OUTPUT_DIR = Path("/root/videos")
STATE_PATH = Path("/root/data/mlbb/last_etalon_montage.json")
MIN_SOURCES = int(os.environ.get("ETALON_MIN_SOURCES", "6"))
MAX_SOURCES = int(os.environ.get("ETALON_MAX_SOURCES", "12"))


def pick_hero() -> str | None:
    forced = (os.environ.get("ETALON_HERO") or os.environ.get("SINGLE_HERO_ID") or "").strip().lower()
    if forced:
        folder = HERO_ROOT / forced
        if folder.is_dir() and any(source_is_real_gameplay(p) for p in folder.glob("*.mp4")):
            return forced
    prefer = [
        h.strip().lower()
        for h in os.environ.get(
            "ETALON_HERO_PREFERENCE",
            "hayabusa,miya,chou,franco,fanny,moskov,benedetta,gusion",
        ).split(",")
        if h.strip()
    ]
    best_id: str | None = None
    best_count = 0
    if not HERO_ROOT.exists():
        return None
    hero_dirs = sorted(HERO_ROOT.iterdir(), key=lambda p: p.name)
    ordered = [HERO_ROOT / h for h in prefer if (HERO_ROOT / h).is_dir()]
    ordered += [d for d in hero_dirs if d.is_dir() and d not in ordered]
    for hero_dir in ordered:
        candidates = list(hero_dir.glob("*.mp4"))
        non_promo = [p for p in candidates if not profile_looks_like_mlbb_edit(p)]
        if len(non_promo) < 2:
            continue
        playable = sum(1 for p in non_promo[:8] if source_is_real_gameplay(p))
        if playable > best_count:
            best_count = playable
            best_id = hero_dir.name
    return best_id


def source_is_real_gameplay(path: Path) -> bool:
    if path_blocked_by_calibration(path):
        return False
    if profile_looks_like_mlbb_edit(path):
        return False
    ok, _reason = source_has_valid_gameplay_window(path, windows=3, window_sec=9.0)
    return ok


def gather_hero_sources(hero_id: str, limit: int) -> list[Path]:
    folder = HERO_ROOT / hero_id
    if not folder.is_dir():
        return []
    files = sorted(folder.glob("*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True)
    files = [p for p in files if not path_blocked_by_calibration(p)]
    whitelisted = [p for p in files if path_whitelisted_by_calibration(p)]
    if whitelisted:
        files = whitelisted + [p for p in files if p not in whitelisted]
    allow_used = os.environ.get("ETALON_ALLOW_USED", "1") == "1"
    if allow_used:
        pool = list(files)
    else:
        fresh = [path for path in files if not is_used(path)]
        pool = fresh if len(fresh) >= MIN_SOURCES else files
    gameplay = [path for path in pool if source_is_real_gameplay(path)]
    return gameplay[:limit]


def build_queue(paths: list[Path], chat_id: str, hero_id: str) -> Path:
    fd, queue_path = tempfile.mkstemp(prefix="etalon-mlbb-", suffix=".txt", dir="/tmp")
    label = f"MLBB etalon | {hero_id.replace('_', ' ').title()}"
    with os.fdopen(fd, "w") as handle:
        for path in paths:
            handle.write(f"{path}|{label}|{chat_id}\n")
    return Path(queue_path)


def main() -> int:
    chat_id = os.environ.get("TG_CHAT_ID", "")
    if not chat_id:
        print("[etalon] TG_CHAT_ID missing")
        return 1

    hero_id = pick_hero()
    if not hero_id:
        print("[etalon] no hero_datasets folder with mp4")
        return 1

    picked = gather_hero_sources(hero_id, MAX_SOURCES)
    if len(picked) < MIN_SOURCES:
        print(f"[etalon] hero={hero_id} only {len(picked)} sources (need {MIN_SOURCES})")
        return 1

    print(f"[etalon] single hero={hero_id} sources={len(picked)}")
    queue_path = build_queue(picked, chat_id, hero_id)
    env = os.environ.copy()
    env.update(
        {
            "QUEUE_FILE": str(queue_path),
            "MAX_SOURCES": str(len(picked)),
            "OUTPUT_DIR": str(OUTPUT_DIR),
            "OUTPUT_BASENAME": f"mlbb_etalon_{hero_id}",
            "ETALON_MONTAGE": "1",
            "SINGLE_HERO_MODE": "1",
            "SINGLE_HERO_ID": hero_id,
            "SEND_TELEGRAM": "1",
            "STRICT_GAMEPLAY": "1",
            "SMART_REQUIRE_UNIFORM_GAMEPLAY": "1",
            "SMART_REJECT_HERO_SHOWCASE": "1",
            "TARGET_DURATION": "45",
            "MIN_FINAL_DURATION": "33",
            "MAX_FINAL_DURATION": "57",
            "MIN_HIGHLIGHTS": "3",
            "MAX_HIGHLIGHTS": "4",
            "SMART_ADD_MUSIC": "0",
            "SMART_GAME_AUDIO_ONLY": "1",
            "SMART_STRIP_MUSIC_BED": "1",
            "SMART_REJECT_TRAINING": "1",
            # TikTok clips almost always have a music bed — strip in ffmpeg, filter only extreme cases.
            "SMART_REJECT_MUSIC_BED": "1",
            "SMART_MAX_TRAINING_INTRO": "0.13",
            "SMART_MAX_MUSIC_BED": "0.72",
            "BLUR_NICKNAME": "0",
            "SMART_MIN_HUD": "17",
            "SMART_MIN_HUD_FRAME_RATE": "0.65",
            "SMART_UNIFORM_MIN_HUD_RATE": "0.72",
            "SMART_MIN_CENTER_MOTION": "0.017",
            "SMART_MAX_CHAT_PANEL": "0.15",
            "SMART_MAX_CENTER_TEXT": "0.12",
            "SMART_MAX_OVERLAY_TEXT": "0.55",
            "SMART_MAX_REJECT_SIM": "0.995",
            "SMART_REJECT_PROMO": "1",
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
                    "hero": hero_id,
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
