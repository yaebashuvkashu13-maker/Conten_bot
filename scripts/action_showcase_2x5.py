#!/usr/bin/env python3
"""
Strict peak showcase: 2 montages × 5 games (MLBB, PUBG, Genshin, Standoff, WoT).
No rescue tiers, no relaxed env — honest refuse if <3 strict segments.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from pipeline_retry import (
    count_ok_jobs,
    job_is_ok,
    load_json_state,
    mark_job,
    pipeline_complete,
    run_until_success,
    save_json_state,
)
from strict_montage_direct import make_strict_montage

ENV_FILE = Path("/root/.video_bot.env")
INBOX = Path("/root/data/mlbb/youtube_nightly/inbox")
LOG = Path("/root/data/mlbb/action_showcase_2x5.log")
STATE_FILE = Path("/root/data/mlbb/action_showcase_2x5_state.json")

GAMES = [
    {"id": "mlbb", "profile": "mobile_legends", "label": "MLBB", "source": "yt_2XbUY9dvS7Y.mp4"},
    {"id": "pubg", "profile": "pubg", "label": "PUBG Metro", "source": "yt_n97cHIR9Qow.mp4", "fallback_source": "yt_FpMs48XOnq0.mp4"},
    {"id": "genshin", "profile": "genshin", "label": "Genshin", "source": "yt_ViQhjTOShrA.mp4", "fallback_source": "yt_NXJuHTKXs2g.mp4"},
    {"id": "standoff", "profile": "standoff", "label": "Standoff 2", "source": "yt_z8ImUR0_x_M.mp4"},
    {"id": "wot", "profile": "wot", "label": "WoT", "source": "yt_dQNh92Po_zE.mp4", "fallback_source": "yt_68K8GrmWil4.mp4"},
]

VARIANTS = [
    {"suffix": "v1", "part": "1/2", "SELECTION_VARIANT": "0"},
    {"suffix": "v2", "part": "2/2", "SELECTION_VARIANT": "2"},
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


def run_job(game: dict, variant: dict, env: dict[str, str], chat_id: str, state: dict) -> int:
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
        run_env = dict(env)
        run_env["SELECTION_VARIANT"] = variant["SELECTION_VARIANT"]
        run_env["OUTPUT_DIR"] = "/root/videos"
        caption = (
            f"⚔️ {game['label']} {variant['part']} — пиковые моменты\n"
            f"Strict gate: все сегменты с метриками, без бега/лута/тишины"
        )
        code, detail = make_strict_montage(
            profile=game["profile"],
            vod=source,
            output_basename=f"strict_{game['id']}_{variant['suffix']}",
            caption=caption,
            env=run_env,
        )
        if code == 0:
            mark_job(state, job_key, status="ok", path=STATE_FILE, output=detail, attempts=attempt)
            log(f"ok {game['label']} {variant['part']} -> {detail}")
            return 0
        log(f"refuse {game['label']} {variant['part']} attempt {attempt}: {detail}")
        mark_job(state, job_key, status="retrying", path=STATE_FILE, error=detail, attempts=attempt)
        return code

    code = run_until_success(job_key, attempt_fn, max_attempts=4, log=log)
    if code == 0:
        return 0
    mark_job(state, job_key, status="failed", path=STATE_FILE)
    log(f"fail {job_key} after strict retries")
    return code


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--reset", action="store_true")
    args = parser.parse_args()

    env = load_env()
    chat_id = env.get("TG_CHAT_ID", "")
    if not env.get("TG_BOT_TOKEN") or not chat_id:
        log("TG_BOT_TOKEN / TG_CHAT_ID missing")
        return 1

    if args.reset and STATE_FILE.exists():
        STATE_FILE.unlink(missing_ok=True)

    state = load_json_state(STATE_FILE)
    state.setdefault("jobs", {})
    state["total_jobs"] = TOTAL_JOBS
    state["completed"] = False
    save_json_state(STATE_FILE, state)

    if pipeline_complete(state, TOTAL_JOBS):
        log(f"already complete ({count_ok_jobs(state)}/{TOTAL_JOBS})")
        return 0

    log(f"strict showcase 5×2 — {count_ok_jobs(state)}/{TOTAL_JOBS} done (no rescue tiers)")
    failures = 0
    for game in GAMES:
        for variant in VARIANTS:
            if run_job(game, variant, env, chat_id, state) != 0:
                failures += 1
            time.sleep(8)

    ok_count = count_ok_jobs(state)
    if ok_count >= TOTAL_JOBS:
        state["completed"] = True
        state["completed_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        save_json_state(STATE_FILE, state)
        log(f"done all {TOTAL_JOBS}")
        return 0

    save_json_state(STATE_FILE, state)
    log(f"incomplete {ok_count}/{TOTAL_JOBS} failures={failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
