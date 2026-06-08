#!/usr/bin/env python3
"""Single queue: MLBB → PUBG → Standoff → Genshin → WoT. Owner preview before send."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from pipeline_inbox import pick_inbox_source
from pipeline_retry import count_ok_jobs, job_is_ok, load_json_state, mark_job, pipeline_complete, run_until_success, save_json_state
from pubg_brawl_direct import resolve_pubg_chat_id
from strict_montage_direct import make_strict_montage

ENV_FILE = Path("/root/.video_bot.env")
INBOX = Path("/root/data/mlbb/youtube_nightly/inbox")
LOG = Path("/root/data/mlbb/pubg_mlbb_pipeline.log")
STATE_FILE = Path("/root/data/mlbb/pubg_mlbb_pipeline_state.json")
PAUSE_FILE = Path("/root/data/mlbb/PAUSED_PIPELINES")

# Priority: MLBB → PUBG → Standoff → Genshin → WoT (owner 2026-06-08)
GAMES = [
    {
        "id": "mobile_legends",
        "profile": "mobile_legends",
        "label": "MLBB",
        "sources": ["yt_E4Dsp53yvv4.mp4"],
    },
    {
        "id": "pubg",
        "profile": "pubg",
        "label": "PUBG",
        "sources": [
            "yt_pJ-X6NdSU9k.mp4",
            "yt_zv3JymSZOb0.mp4",
            "yt_FpMs48XOnq0.mp4",
            "yt_n97cHIR9Qow.mp4",
        ],
    },
    {
        "id": "standoff",
        "profile": "standoff",
        "label": "Standoff",
        "sources": [
            "yt_ou2CbjDp2Yc.mp4",
            "yt_z8ImUR0_x_M.mp4",
        ],
    },
    {
        "id": "genshin",
        "profile": "genshin",
        "label": "Genshin",
        "sources": ["yt_i67K34fQa9I.mp4"],
    },
    {
        "id": "wot",
        "profile": "wot",
        "label": "WoT",
        "sources": ["yt_QbBwJJTio6A.mp4"],
    },
]

TOTAL_JOBS = len(GAMES)


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


def pick_source(game: dict, attempt: int) -> Path | None:
    return pick_inbox_source(game, attempt, inbox=INBOX, all_games=GAMES)


def run_game(game: dict, env: dict[str, str], state: dict) -> int:
    job_key = f"queue:{game['id']}"
    if job_is_ok(state, job_key):
        log(f"skip done {job_key}")
        return 0

    def attempt_fn(attempt: int) -> int:
        source = pick_source(game, attempt)
        if source is None:
            log(f"REFUSED: game={game['label']}, reason=no_vod, visual_passed=0/0")
            return 2
        run_env = dict(env)
        # Preview always to owner — never auto-send to PUBG colleague chat
        owner_chat = env.get("TG_CHAT_ID", "")
        run_env["TG_CHAT_ID"] = owner_chat
        run_env["SEND_TELEGRAM"] = "0"
        run_env["OWNER_PREVIEW_REQUIRED"] = "1"
        run_env["HIGHLIGHT_SCORER"] = "1"
        run_env["HIGHLIGHT_USE_OWNER_ANCHORS"] = "0"
        run_env["HIGHLIGHT_MAX_STAGE1"] = "48"
        run_env["HIGHLIGHT_MAX_PANN_PROBE"] = "32"
        run_env["HIGHLIGHT_MLBB_SKIP_INTRO_SEC"] = "300"
        run_env["HIGHLIGHT_ACTION_PEAK_LIMIT"] = "40"
        run_env["HIGHLIGHT_CLIP_DISABLED"] = "0"
        run_env["INTELLICLIP"] = "1"
        run_env["INTELLICLIP_STAGE1"] = "1"
        run_env["INTELLICLIP_FUSION"] = "0"
        run_env["INTELLICLIP_MAX_CLIPS"] = "4"
        run_env.setdefault("CONTENT_BOT_REPO", "/root/content_bot_ml")
        run_env.setdefault(
            "HIGHLIGHT_EXEMPLAR_ROOT",
            f"{run_env['CONTENT_BOT_REPO']}/data/highlight_exemplars",
        )
        label_root = f"{run_env['CONTENT_BOT_REPO']}/data"
        for env_key, fname in (
            ("PUBG_OWNER_LABELS_PATH", "pubg_owner_labels.json"),
            ("STANDOFF_OWNER_LABELS_PATH", "standoff_owner_labels.json"),
            ("MLBB_OWNER_LABELS_PATH", "mobile_legends_owner_labels.json"),
            ("GENSHIN_OWNER_LABELS_PATH", "genshin_owner_labels.json"),
            ("WOT_OWNER_LABELS_PATH", "wot_owner_labels.json"),
        ):
            run_env.setdefault(env_key, f"{label_root}/{fname}")

        log(f"queue {game['label']} attempt={attempt} vod={source.name}")
        code, detail = make_strict_montage(
            profile=game["profile"],
            vod=source,
            output_basename=f"queue_{game['id']}",
            caption=f"{game['label']} peak montage",
            env=run_env,
        )
        log(detail)
        if code == 3:
            # Visual OK, awaiting owner — treat as job done for pipeline (no retry spam)
            mark_job(state, job_key, status="ok", path=STATE_FILE, output=detail, attempts=attempt)
            return 0
        if code == 0:
            mark_job(state, job_key, status="ok", path=STATE_FILE, output=detail, attempts=attempt)
            return 0
        mark_job(state, job_key, status="retrying", path=STATE_FILE, error=detail, attempts=attempt)
        return code

    return run_until_success(job_key, attempt_fn, max_attempts=6, log=log)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--reset", action="store_true")
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()

    if PAUSE_FILE.exists() and "pubg_mlbb_pipeline.py" in PAUSE_FILE.read_text():
        log("paused by PAUSED_PIPELINES")
        return 0

    env = load_env()
    if not env.get("TG_BOT_TOKEN") or not env.get("TG_CHAT_ID"):
        log("REFUSED: pipeline, reason=missing_telegram_env, visual_passed=0/0")
        return 1

    if args.reset and STATE_FILE.exists():
        STATE_FILE.unlink(missing_ok=True)

    state = load_json_state(STATE_FILE)
    state.setdefault("jobs", {})
    state["total_jobs"] = TOTAL_JOBS
    save_json_state(STATE_FILE, state)

    if pipeline_complete(state, TOTAL_JOBS):
        log(f"queue complete ({count_ok_jobs(state)}/{TOTAL_JOBS})")
        return 0

    log(f"pubg_mlbb queue — {count_ok_jobs(state)}/{TOTAL_JOBS}")
    failures = 0
    for game in GAMES:
        if run_game(game, env, state) != 0:
            failures += 1

    ok = count_ok_jobs(state)
    save_json_state(STATE_FILE, state)
    if ok >= TOTAL_JOBS:
        state["completed"] = True
        state["completed_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        save_json_state(STATE_FILE, state)
        return 0
    log(f"incomplete {ok}/{TOTAL_JOBS} failures={failures}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
