#!/usr/bin/env python3
"""Single queue: PUBG then MLBB. No sendVideo until owner approves visual proof."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from pipeline_retry import count_ok_jobs, job_is_ok, load_json_state, mark_job, pipeline_complete, run_until_success, save_json_state
from pubg_brawl_direct import resolve_pubg_chat_id
from strict_montage_direct import make_strict_montage

ENV_FILE = Path("/root/.video_bot.env")
INBOX = Path("/root/data/mlbb/youtube_nightly/inbox")
LOG = Path("/root/data/mlbb/pubg_mlbb_pipeline.log")
STATE_FILE = Path("/root/data/mlbb/pubg_mlbb_pipeline_state.json")
PAUSE_FILE = Path("/root/data/mlbb/PAUSED_PIPELINES")

# PUBG + Standoff (highlight scorer priority). MLBB/Genshin/WoT after owner OK.
GAMES = [
    {
        "id": "pubg",
        "profile": "pubg",
        "label": "PUBG",
        # zv3J first: stable 3+ PANNs peaks; FpMs48 second for seed expansion
        "sources": ["yt_zv3JymSZOb0.mp4", "yt_FpMs48XOnq0.mp4", "yt_n97cHIR9Qow.mp4"],
    },
    {
        "id": "standoff",
        "profile": "standoff",
        "label": "Standoff",
        "sources": ["yt_z8ImUR0_x_M.mp4"],
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
    sources = game.get("sources") or []
    existing = [INBOX / s for s in sources if (INBOX / s).exists()]
    if not existing:
        return None
    return existing[min(attempt - 1, len(existing) - 1)]


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
        run_env["HIGHLIGHT_ANCHOR_FIRST"] = "1"
        run_env["HIGHLIGHT_MAX_STAGE1"] = "48"
        run_env["HIGHLIGHT_MAX_PANN_PROBE"] = "32"
        run_env["HIGHLIGHT_CLIP_DISABLED"] = "1"
        if game["id"] == "pubg" and "FpMs48XOnq0" in source.name:
            run_env["HIGHLIGHT_SEED_STARTS"] = "480,120,390,1080,1110,630,780,870,900"
        elif game["id"] == "pubg" and "zv3JymSZOb0" in source.name:
            run_env["HIGHLIGHT_SEED_STARTS"] = "900,870,780,630,390"

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
