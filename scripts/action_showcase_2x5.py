#!/usr/bin/env python3
"""
10 montages: 2 action-heavy cuts per game (MLBB, PUBG, Genshin, Standoff, WoT).
Uses downloaded inbox VODs + strict combat env from montage_env.
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
INBOX = Path("/root/data/mlbb/youtube_nightly/inbox")
LOG = Path("/root/data/mlbb/action_showcase_2x5.log")
HISTORY_ROOT = Path("/tmp/action_showcase_2x5_history")

GAMES = [
    {
        "id": "mlbb",
        "profile": "mobile_legends",
        "label": "MLBB",
        "source": "yt_2XbUY9dvS7Y.mp4",
    },
    {
        "id": "pubg",
        "profile": "pubg",
        "label": "PUBG Metro",
        "source": "yt_FpMs48XOnq0.mp4",
    },
    {
        "id": "genshin",
        "profile": "genshin",
        "label": "Genshin",
        "source": "yt_ViQhjTOShrA.mp4",
        "fallback_source": "yt_NXJuHTKXs2g.mp4",
    },
    {
        "id": "standoff",
        "profile": "standoff",
        "label": "Standoff 2",
        "source": "yt_z8ImUR0_x_M.mp4",
    },
    {
        "id": "wot",
        "profile": "world_of_tanks",
        "label": "WoT",
        "source": "yt_68K8GrmWil4.mp4",
    },
]

VARIANTS = [
    {
        "suffix": "v1",
        "part": "1/2",
        "SELECTION_VARIANT": "0",
        "extra": {},
    },
    {
        "suffix": "v2",
        "part": "2/2",
        "SELECTION_VARIANT": "2",
        "extra": {
            "SMART_MLBB_PEAK_PERCENTILE": "48",
            "SMART_PUBG_PEAK_PERCENTILE": "32",
            "SMART_GENSHIN_PEAK_PERCENTILE": "34",
            "SMART_STANDOFF_PEAK_PERCENTILE": "28",
            "SMART_WOT_PEAK_PERCENTILE": "30",
        },
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


def wait_editor() -> None:
    while True:
        if not subprocess.run(
            ["pgrep", "-f", "smart_video_editor.py"],
            capture_output=True,
        ).returncode:
            time.sleep(45)
            continue
        break


def run_one(
    source: Path,
    game: dict,
    variant: dict,
    env: dict[str, str],
    chat_id: str,
) -> int:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from montage_env import profile_montage_env

    profile = game["profile"]
    gid = game["id"]
    with tempfile.NamedTemporaryFile("w", delete=False, suffix=".queue.txt") as tmp:
        tmp.write(f"{source.resolve()}|{game['label']}|{chat_id}\n")
        queue_path = tmp.name

    history = HISTORY_ROOT / f"{gid}.json"
    run_env = os.environ.copy()
    for key, value in env.items():
        run_env.setdefault(key, value)
    run_env.update(profile_montage_env(profile))
    run_env.update(variant.get("extra") or {})
    run_env.update(
        {
            "QUEUE_FILE": queue_path,
            "MAX_SOURCES": "1",
            "SINGLE_SOURCE_MODE": "1",
            "SEND_TELEGRAM": "1",
            "SMART_BLOCKING_LOCK": "1",
            "OUTPUT_DIR": "/root/videos",
            "DEFAULT_GAME_PROFILE": profile,
            "QUEUE_GAME_PROFILE": profile,
            "OVERNIGHT_FRESH_SEGMENTS": "0",
            "SEGMENT_HISTORY_FILE": str(history),
            "SELECTION_VARIANT": str(variant["SELECTION_VARIANT"]),
            "OUTPUT_BASENAME": f"showcase_{gid}_{variant['suffix']}",
            "MONTAGE_CAPTION": (
                f"⚔️ {game['label']} {variant['part']} — только экшен\n"
                f"Бои / перестрелки / боссы, без пустой езды и лута"
            ),
        }
    )
    out_slug = f"showcase_{gid}_{variant['suffix']}"
    before = {p.name for p in Path("/root/videos").glob(f"{out_slug}_*.mp4")}

    try:
        log(f"start {game['label']} {variant['part']} ({source.name})")
        completed = subprocess.run(
            [sys.executable, str(PROCESSOR)],
            env=run_env,
            capture_output=True,
            text=True,
            timeout=int(float(env.get("SMART_MAKE_TIMEOUT_MAX_SEC", "14400"))),
        )
        tail = (completed.stderr or completed.stdout or "")[-800:]
        new_files = [p for p in Path("/root/videos").glob(f"{out_slug}_*.mp4") if p.name not in before]
        if completed.returncode != 0:
            log(f"fail {gid} {variant['suffix']} rc={completed.returncode} tail={tail}")
            return completed.returncode
        if not new_files:
            log(f"fail {gid} {variant['suffix']} no output tail={tail}")
            return 3
        log(f"ok {game['label']} {variant['part']} -> {new_files[-1].name}")
        return 0
    finally:
        Path(queue_path).unlink(missing_ok=True)


def main() -> int:
    env = load_env()
    chat_id = env.get("TG_CHAT_ID", "")
    if not env.get("TG_BOT_TOKEN") or not chat_id:
        log("TG_BOT_TOKEN / TG_CHAT_ID missing")
        return 1

    HISTORY_ROOT.mkdir(parents=True, exist_ok=True)
    log("action showcase 2x5 — wait for idle editor")
    wait_editor()

    failures = 0
    for game in GAMES:
        source = INBOX / game["source"]
        if not source.exists():
            fallback_name = game.get("fallback_source")
            if fallback_name:
                source = INBOX / fallback_name
            if not source.exists():
                log(f"skip {game['id']}: missing {game['source']}")
                failures += 1
                continue
        for variant in VARIANTS:
            wait_editor()
            code = run_one(source, game, variant, env, chat_id)
            if code != 0 and game.get("fallback_source"):
                alt = INBOX / game["fallback_source"]
                if alt.exists() and alt != source:
                    log(f"retry {game['id']} with fallback {alt.name}")
                    wait_editor()
                    code = run_one(alt, game, variant, env, chat_id)
            if code != 0:
                failures += 1
            time.sleep(8)

    log(f"done failures={failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
