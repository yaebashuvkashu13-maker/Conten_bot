#!/usr/bin/env python3
"""
Four PUBG Metro montages from one long VOD — different selection harshness.
Sends each to TG_CHAT_ID with labels 1/4 … 4/4 for owner feedback.
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
DEFAULT_SOURCE = Path("/root/data/mlbb/youtube_nightly/inbox/yt_559CEnq-8-o.mp4")
HISTORY = Path("/tmp/pubg_harshness_demo_history.json")
LOG = Path("/root/data/mlbb/pubg_harshness_demo.log")

PRESETS = [
    {
        "id": "soft",
        "label": "1/4 мягко",
        "SMART_PUBG_PEAK_PERCENTILE": "72",
        "SMART_BURST_WEIGHT": "0.18",
        "MIN_HIGHLIGHTS": "3",
        "MAX_HIGHLIGHTS": "3",
        "SELECTION_VARIANT": "0",
    },
    {
        "id": "balanced",
        "label": "2/4 сбалансировано",
        "SMART_PUBG_PEAK_PERCENTILE": "60",
        "SMART_BURST_WEIGHT": "0.34",
        "MIN_HIGHLIGHTS": "4",
        "MAX_HIGHLIGHTS": "4",
        "SELECTION_VARIANT": "1",
    },
    {
        "id": "hard",
        "label": "3/4 жёстко",
        "SMART_PUBG_PEAK_PERCENTILE": "52",
        "SMART_BURST_WEIGHT": "0.42",
        "MIN_HIGHLIGHTS": "4",
        "MAX_HIGHLIGHTS": "5",
        "SELECTION_VARIANT": "2",
    },
    {
        "id": "max",
        "label": "4/4 максимум перестрелок",
        "SMART_PUBG_PEAK_PERCENTILE": "45",
        "SMART_BURST_WEIGHT": "0.52",
        "MIN_HIGHLIGHTS": "5",
        "MAX_HIGHLIGHTS": "5",
        "SELECTION_VARIANT": "3",
    },
]


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
            "OUTPUT_BASENAME": f"pubg_harsh_{preset['id']}",
            "MONTAGE_CAPTION": (
                f"🎯 PUBG Metro — {preset['label']}\n"
                f"Один длинный VOD, разная «жёсткость» отбора перестрелок.\n"
                f"Напишите, какой уровень (1–4) зашёл больше."
            ),
        }
    )
    for key in (
        "SMART_PUBG_PEAK_PERCENTILE",
        "SMART_BURST_WEIGHT",
        "MIN_HIGHLIGHTS",
        "MAX_HIGHLIGHTS",
        "SELECTION_VARIANT",
    ):
        run_env[key] = preset[key]

    try:
        log(f"start {preset['label']} ({preset['id']})")
        completed = subprocess.run(
            [sys.executable, str(PROCESSOR)],
            env=run_env,
            capture_output=True,
            text=True,
            timeout=int(float(env.get("SMART_MAKE_TIMEOUT_MAX_SEC", "14400"))),
        )
        if completed.returncode != 0:
            log(f"fail {preset['id']} rc={completed.returncode} tail={(completed.stderr or '')[-400:]}")
        else:
            log(f"ok {preset['label']}")
        return completed.returncode
    finally:
        Path(queue_path).unlink(missing_ok=True)


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    args = parser.parse_args()

    if not args.source.exists():
        log(f"source missing: {args.source}")
        return 1

    env = load_env()
    chat_id = env.get("TG_CHAT_ID", "")
    if not env.get("TG_BOT_TOKEN") or not chat_id:
        log("TG_BOT_TOKEN / TG_CHAT_ID missing")
        return 1

    if HISTORY.exists():
        HISTORY.unlink()

    log(f"pubg harshness demo source={args.source.name} chat={chat_id[:8]}…")
    for preset in PRESETS:
        code = run_one(args.source, preset, env, chat_id)
        if code != 0:
            log(f"stopped after {preset['id']}")
            return code
        time.sleep(8)

    log("all 4 montages done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
