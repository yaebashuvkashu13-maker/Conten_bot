#!/usr/bin/env python3
"""
Four MLBB montages from one VOD — increasing fight intensity (culmination).
Queue order: 1 → 2 → 3 → 4, each sent to Telegram when ready.
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
DEFAULT_SOURCE = Path("/root/data/mlbb/youtube_nightly/inbox/yt_2XbUY9dvS7Y.mp4")
HISTORY = Path("/tmp/mlbb_intensity_demo_history.json")
LOG = Path("/root/data/mlbb/mlbb_intensity_demo.log")

PRESETS = [
    {
        "id": "v1_soft",
        "label": "1/4 — мягче, 4 боя",
        "SMART_MLBB_PEAK_PERCENTILE": "62",
        "SMART_MIN_CENTER_MOTION": "0.016",
        "SMART_MIN_MINIMAP_DELTA": "0.009",
        "MIN_HIGHLIGHTS": "4",
        "MAX_HIGHLIGHTS": "4",
        "SELECTION_VARIANT": "0",
    },
    {
        "id": "v2_mid",
        "label": "2/4 — средняя кульминация, 5 боёв",
        "SMART_MLBB_PEAK_PERCENTILE": "54",
        "SMART_MIN_CENTER_MOTION": "0.018",
        "SMART_MIN_MINIMAP_DELTA": "0.011",
        "MIN_HIGHLIGHTS": "5",
        "MAX_HIGHLIGHTS": "5",
        "SELECTION_VARIANT": "1",
    },
    {
        "id": "v3_hard",
        "label": "3/4 — жёстче, только пики",
        "SMART_MLBB_PEAK_PERCENTILE": "46",
        "SMART_MIN_CENTER_MOTION": "0.020",
        "SMART_MIN_MINIMAP_DELTA": "0.012",
        "MIN_HIGHLIGHTS": "5",
        "MAX_HIGHLIGHTS": "5",
        "SELECTION_VARIANT": "2",
    },
    {
        "id": "v4_max",
        "label": "4/4 — макс суета, 6 боёв",
        "SMART_MLBB_PEAK_PERCENTILE": "38",
        "SMART_MIN_CENTER_MOTION": "0.022",
        "SMART_MIN_MINIMAP_DELTA": "0.013",
        "MIN_HIGHLIGHTS": "6",
        "MAX_HIGHLIGHTS": "6",
        "MAX_FINAL_DURATION": "57",
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
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from montage_env import mlbb_combat_env

    with tempfile.NamedTemporaryFile("w", delete=False, suffix=".queue.txt") as tmp:
        tmp.write(f"{source.resolve()}|MLBB|{chat_id}\n")
        queue_path = tmp.name

    run_env = os.environ.copy()
    for key, value in env.items():
        run_env.setdefault(key, value)
    run_env.update(mlbb_combat_env())
    run_env.update(
        {
            "QUEUE_FILE": queue_path,
            "MAX_SOURCES": "1",
            "SINGLE_SOURCE_MODE": "1",
            "SEND_TELEGRAM": "1",
            "OUTPUT_DIR": "/root/videos",
            "DEFAULT_GAME_PROFILE": "mobile_legends",
            "QUEUE_GAME_PROFILE": "mobile_legends",
            "SMART_BLOCKING_LOCK": "1",
            "SEGMENT_HISTORY_FILE": str(HISTORY),
            "OUTPUT_BASENAME": f"mlbb_intensity_{preset['id']}",
            "MONTAGE_CAPTION": (
                f"⚔️ MLBB — {preset['label']}\n"
                f"Тот же стрим, разная кульминация. Напишите 1–4."
            ),
        }
    )
    for key, value in preset.items():
        if key not in ("id", "label"):
            run_env[key] = str(value)

    out_dir = Path(run_env.get("OUTPUT_DIR", "/root/videos"))
    slug = f"mlbb_intensity_{preset['id']}"
    before = {p.name for p in out_dir.glob(f"{slug}_*.mp4")}

    try:
        log(f"start {preset['label']}")
        completed = subprocess.run(
            [sys.executable, str(PROCESSOR)],
            env=run_env,
            capture_output=True,
            text=True,
            timeout=int(float(env.get("SMART_MAKE_TIMEOUT_MAX_SEC", "14400"))),
        )
        new_files = [p for p in out_dir.glob(f"{slug}_*.mp4") if p.name not in before]
        tail = (completed.stderr or completed.stdout or "")[-800:]
        if completed.returncode != 0:
            log(f"fail {preset['id']} rc={completed.returncode} tail={tail}")
            return completed.returncode
        if not new_files:
            log(f"fail {preset['id']} no output tail={tail}")
            return 3
        log(f"ok {preset['label']} -> {new_files[-1].name}")
        return 0
    finally:
        Path(queue_path).unlink(missing_ok=True)


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--only", type=str, default="", help="preset id subset")
    args = parser.parse_args()

    env = load_env()
    chat_id = env.get("TG_CHAT_ID", "")
    if not env.get("TG_BOT_TOKEN") or not chat_id:
        log("TG_BOT_TOKEN / TG_CHAT_ID missing")
        return 1
    if not args.source.is_file():
        log(f"source missing: {args.source}")
        return 1

    only = {x.strip() for x in args.only.split(",") if x.strip()}
    presets = [p for p in PRESETS if not only or p["id"] in only]
    log(f"MLBB intensity demo: {args.source.name} presets={len(presets)}")

    for preset in presets:
        rc = run_one(args.source, preset, env, chat_id)
        if rc != 0:
            log(f"stop after {preset['id']} rc={rc}")
            return rc
        time.sleep(8)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
