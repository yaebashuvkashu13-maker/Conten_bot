#!/usr/bin/env python3
"""
10 montages: 2 action-heavy cuts per game (MLBB, PUBG, Genshin, Standoff, WoT).
Uses downloaded inbox VODs + strict combat env from montage_env.
Retries failed jobs automatically; state file enables resume after crash.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from pipeline_retry import (
    count_ok_jobs,
    job_is_ok,
    load_json_state,
    mark_job,
    pipeline_complete,
    retry_sleep_sec,
    run_until_success,
    save_json_state,
)

ENV_FILE = Path("/root/.video_bot.env")
PROCESSOR = Path("/usr/local/bin/smart_video_editor.py")
INBOX = Path("/root/data/mlbb/youtube_nightly/inbox")
LOG = Path("/root/data/mlbb/action_showcase_2x5.log")
STATE_FILE = Path("/root/data/mlbb/action_showcase_2x5_state.json")
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

TOTAL_JOBS = len(GAMES) * len(VARIANTS)


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
    while subprocess.run(["pgrep", "-f", "smart_video_editor.py"], capture_output=True).returncode == 0:
        time.sleep(45)


def resolve_source(game: dict, attempt: int) -> Path | None:
    primary = INBOX / game["source"]
    fallback_name = game.get("fallback_source")
    fallback = INBOX / fallback_name if fallback_name else None
    if attempt >= 3 and fallback and fallback.exists():
        return fallback
    if primary.exists():
        return primary
    if fallback and fallback.exists():
        return fallback
    return None


def run_one_attempt(
    source: Path,
    game: dict,
    variant: dict,
    env: dict[str, str],
    chat_id: str,
    attempt: int,
) -> tuple[int, str]:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from montage_env import profile_montage_env, relaxed_montage_env

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
    if attempt >= 2:
        run_env.update(relaxed_montage_env(profile))
    if attempt >= 3:
        run_env["OVERNIGHT_FRESH_SEGMENTS"] = "1"
        run_env["SELECTION_VARIANT"] = str((int(variant["SELECTION_VARIANT"]) + attempt) % 4)
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
            "SEGMENT_HISTORY_FILE": str(history),
            "SELECTION_VARIANT": run_env.get("SELECTION_VARIANT", str(variant["SELECTION_VARIANT"])),
            "OUTPUT_BASENAME": f"showcase_{gid}_{variant['suffix']}",
            "MONTAGE_CAPTION": (
                f"⚔️ {game['label']} {variant['part']} — только экшен\n"
                f"Бои / перестрелки / боссы, без пустой езды и лута"
            ),
        }
    )
    if attempt < 3:
        run_env.setdefault("OVERNIGHT_FRESH_SEGMENTS", "0")

    out_slug = f"showcase_{gid}_{variant['suffix']}"
    out_dir = Path(run_env["OUTPUT_DIR"])
    before = {p.name for p in out_dir.glob(f"{out_slug}_*.mp4")}

    try:
        log(f"start {game['label']} {variant['part']} ({source.name}) attempt={attempt}")
        completed = subprocess.run(
            [sys.executable, str(PROCESSOR)],
            env=run_env,
            capture_output=True,
            text=True,
            timeout=int(float(env.get("SMART_MAKE_TIMEOUT_MAX_SEC", "14400"))),
        )
        tail = (completed.stderr or completed.stdout or "")[-800:]
        new_files = [p for p in out_dir.glob(f"{out_slug}_*.mp4") if p.name not in before]
        if completed.returncode != 0:
            return completed.returncode, tail
        if not new_files:
            return 3, tail
        log(f"ok {game['label']} {variant['part']} -> {new_files[-1].name}")
        return 0, new_files[-1].name
    finally:
        Path(queue_path).unlink(missing_ok=True)


def run_job(
    game: dict,
    variant: dict,
    env: dict[str, str],
    chat_id: str,
    state: dict,
) -> int:
    job_key = f"{game['id']}:{variant['suffix']}"
    if job_is_ok(state, job_key):
        log(f"skip done {job_key}")
        return 0

    def attempt_fn(attempt: int) -> int:
        wait_editor()
        source = resolve_source(game, attempt)
        if source is None:
            log(f"fail {job_key}: no source file")
            return 2
        code, detail = run_one_attempt(source, game, variant, env, chat_id, attempt)
        if code == 0:
            mark_job(
                state,
                job_key,
                status="ok",
                path=STATE_FILE,
                output=detail,
                attempts=attempt,
            )
            return 0
        mark_job(
            state,
            job_key,
            status="retrying",
            path=STATE_FILE,
            error=detail,
            attempts=attempt,
        )
        return code

    code = run_until_success(job_key, attempt_fn, log=log)
    if code == 0:
        return 0
    mark_job(state, job_key, status="failed", path=STATE_FILE, attempts=0)
    log(f"fail {job_key} after all retries")
    return code


def init_state(reset: bool) -> dict:
    if reset and STATE_FILE.exists():
        STATE_FILE.unlink(missing_ok=True)
    state = load_json_state(STATE_FILE)
    state.setdefault("jobs", {})
    state["total_jobs"] = TOTAL_JOBS
    state["started_at"] = state.get("started_at") or time.strftime("%Y-%m-%d %H:%M:%S")
    state["completed"] = False
    save_json_state(STATE_FILE, state)
    return state


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--resume", action="store_true", help="resume from state file (default behaviour)")
    parser.add_argument("--reset", action="store_true", help="clear state and start over")
    args = parser.parse_args()

    env = load_env()
    chat_id = env.get("TG_CHAT_ID", "")
    if not env.get("TG_BOT_TOKEN") or not chat_id:
        log("TG_BOT_TOKEN / TG_CHAT_ID missing")
        return 1

    HISTORY_ROOT.mkdir(parents=True, exist_ok=True)
    state = init_state(reset=args.reset)

    if pipeline_complete(state, TOTAL_JOBS):
        log(f"already complete ({count_ok_jobs(state)}/{TOTAL_JOBS})")
        return 0

    log(f"action showcase 2x5 — {count_ok_jobs(state)}/{TOTAL_JOBS} done")
    wait_editor()

    failures = 0
    for game in GAMES:
        for variant in VARIANTS:
            code = run_job(game, variant, env, chat_id, state)
            if code != 0:
                failures += 1
            time.sleep(8)

    ok_count = count_ok_jobs(state)
    if ok_count >= TOTAL_JOBS:
        state["completed"] = True
        state["completed_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        save_json_state(STATE_FILE, state)
        log(f"done all {TOTAL_JOBS} montages")
        return 0

    save_json_state(STATE_FILE, state)
    log(f"incomplete {ok_count}/{TOTAL_JOBS} failures={failures} — watchdog will retry")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
