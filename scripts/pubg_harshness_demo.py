#!/usr/bin/env python3
"""
Four PUBG Metro montages from one long VOD — increasing COMBAT density (more gunfights).
«Жёсткость» = больше коротких клипов с перестрелками, меньше бега/лута.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ENV_FILE = Path("/root/.video_bot.env")
PROCESSOR = Path("/usr/local/bin/smart_video_editor.py")
DEFAULT_SOURCE = Path("/root/data/mlbb/youtube_nightly/inbox/yt_FpMs48XOnq0.mp4")
FALLBACK_URL = "https://www.youtube.com/live/FpMs48XOnq0"
HISTORY = Path("/tmp/pubg_harshness_demo_history.json")
LOG = Path("/root/data/mlbb/pubg_harshness_demo.log")

# Peak percentile ↓ = больше боевых пиков; highlights ↑ = больше событий в ролике.
PRESETS = [
    {
        "id": "v2_gun4",
        "label": "1/4 — 4 перестрелки",
        "SMART_PUBG_PEAK_PERCENTILE": "46",
        "SMART_PUBG_SUSTAIN_PERCENTILE": "34",
        "SMART_PUBG_COMBAT_MIN": "0.18",
        "SMART_BURST_WEIGHT": "0.40",
        "SMART_PUBG_CLIP_MIN_SEC": "7",
        "SMART_PUBG_CLIP_MAX_SEC": "11",
        "MIN_HIGHLIGHTS": "4",
        "MAX_HIGHLIGHTS": "4",
        "SELECTION_VARIANT": "0",
    },
    {
        "id": "v2_gun5",
        "label": "2/4 — 5 перестрелок",
        "SMART_PUBG_PEAK_PERCENTILE": "38",
        "SMART_PUBG_SUSTAIN_PERCENTILE": "30",
        "SMART_PUBG_COMBAT_MIN": "0.20",
        "SMART_BURST_WEIGHT": "0.46",
        "SMART_PUBG_CLIP_MIN_SEC": "7",
        "SMART_PUBG_CLIP_MAX_SEC": "10",
        "MIN_HIGHLIGHTS": "5",
        "MAX_HIGHLIGHTS": "5",
        "SELECTION_VARIANT": "1",
    },
    {
        "id": "v2_gun5h",
        "label": "3/4 — 5 боёв, только пики",
        "SMART_PUBG_PEAK_PERCENTILE": "30",
        "SMART_PUBG_SUSTAIN_PERCENTILE": "26",
        "SMART_PUBG_COMBAT_MIN": "0.22",
        "SMART_BURST_WEIGHT": "0.52",
        "SMART_PUBG_CLIP_MIN_SEC": "6.5",
        "SMART_PUBG_CLIP_MAX_SEC": "10",
        "MIN_HIGHLIGHTS": "5",
        "MAX_HIGHLIGHTS": "5",
        "SELECTION_VARIANT": "2",
    },
    {
        "id": "v2_gun6",
        "label": "4/4 — 6 боёв, макс суета",
        "SMART_PUBG_PEAK_PERCENTILE": "22",
        "SMART_PUBG_SUSTAIN_PERCENTILE": "22",
        "SMART_PUBG_COMBAT_MIN": "0.24",
        "SMART_BURST_WEIGHT": "0.58",
        "SMART_PUBG_CLIP_MIN_SEC": "6",
        "SMART_PUBG_CLIP_MAX_SEC": "9.5",
        "MIN_HIGHLIGHTS": "6",
        "MAX_HIGHLIGHTS": "6",
        "MAX_FINAL_DURATION": "57",
        "SELECTION_VARIANT": "3",
    },
]

ENV_KEYS = (
    "SMART_PUBG_PEAK_PERCENTILE",
    "SMART_PUBG_SUSTAIN_PERCENTILE",
    "SMART_PUBG_COMBAT_MIN",
    "SMART_PUBG_MOTION_PERCENTILE",
    "SMART_PUBG_AUDIO_PERCENTILE",
    "SMART_BURST_WEIGHT",
    "SMART_PUBG_CLIP_MIN_SEC",
    "SMART_PUBG_CLIP_MAX_SEC",
    "MIN_HIGHLIGHTS",
    "MAX_HIGHLIGHTS",
    "MAX_FINAL_DURATION",
    "SELECTION_VARIANT",
)


def load_env(path: Path = ENV_FILE) -> dict[str, str]:
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key.strip()] = value.strip().strip('"').strip("'")
    return env


def log(msg: str) -> None:
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n"
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as handle:
        handle.write(line)
    print(line, end="")


def run_one(source: Path, preset: dict, env: dict[str, str], chat_id: str) -> int:
    with tempfile.NamedTemporaryFile("w", delete=False, suffix=".queue.txt") as tmp:
        tmp.write(f"{source.resolve()}|PUBG Metro Royale|{chat_id}\n")
        queue_path = tmp.name

    run_env = os.environ.copy()
    for key, value in env.items():
        run_env.setdefault(key, value)
    run_env.update(
        {
            "QUEUE_FILE": queue_path,
            "MAX_SOURCES": "1",
            "SINGLE_SOURCE_MODE": "1",
            "SEND_TELEGRAM": "1",
            "OUTPUT_DIR": "/root/videos",
            "DEFAULT_GAME_PROFILE": "pubg",
            "QUEUE_GAME_PROFILE": "pubg",
            "STRICT_GAMEPLAY": "0",
            "SEGMENT_HISTORY_FILE": str(HISTORY),
            "SMART_GAME_AUDIO_ONLY": "0",
            "SMART_ADD_MUSIC": "0",
            "SMART_STRIP_MUSIC_BED": "0",
            "SMART_BLOCKING_LOCK": "1",
            "OUTPUT_BASENAME": f"pubg_harsh_{preset['id']}",
            "MONTAGE_CAPTION": (
                f"🔫 PUBG Metro — {preset['label']}\n"
                f"Больше коротких клипов с перестрелками (v2). Напишите 1–4."
            ),
        }
    )
    for key in ENV_KEYS:
        if key in preset:
            run_env[key] = str(preset[key])

    out_dir = Path(run_env.get("OUTPUT_DIR", "/root/videos"))
    slug = f"pubg_harsh_{preset['id']}"
    before = {p.name for p in out_dir.glob(f"{slug}_*.mp4")}

    try:
        log(f"start {preset['label']} ({preset['id']})")
        completed = subprocess.run(
            [sys.executable, str(PROCESSOR)],
            env=run_env,
            capture_output=True,
            text=True,
            timeout=int(float(env.get("SMART_MAKE_TIMEOUT_MAX_SEC", "14400"))),
        )
        new_files = [p for p in out_dir.glob(f"{slug}_*.mp4") if p.name not in before]
        tail = (completed.stderr or completed.stdout or "")[-600:]
        if completed.returncode != 0:
            log(f"fail {preset['id']} rc={completed.returncode} tail={tail}")
        elif not new_files:
            log(f"fail {preset['id']} no output file tail={tail}")
            return 3
        else:
            log(f"ok {preset['label']} -> {new_files[-1].name}")
        return completed.returncode
    finally:
        Path(queue_path).unlink(missing_ok=True)


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    args = parser.parse_args()

    env = load_env()
    if not args.source.exists():
        log("downloading Metro Royale source…")
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from youtube_download import download_one

        args.source.parent.mkdir(parents=True, exist_ok=True)
        try:
            args.source = download_one(FALLBACK_URL, args.source.parent, env)
            log(f"downloaded {args.source}")
        except Exception as exc:
            log(f"download failed: {exc}")
            return 1

    chat_id = env.get("TG_CHAT_ID", "")
    if not env.get("TG_BOT_TOKEN") or not chat_id:
        log("TG_BOT_TOKEN / TG_CHAT_ID missing")
        return 1

    if HISTORY.exists():
        HISTORY.unlink()

    log(f"v2 combat demo source={args.source.name}")
    for preset in PRESETS:
        code = run_one(args.source, preset, env, chat_id)
        if code != 0:
            log(f"stopped after {preset['id']}")
            return code
        time.sleep(8)

    log("all 4 combat montages done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
