#!/usr/bin/env python3
"""Ten PUBG brawl montages — direct cuts from verified fight windows, zero segment reuse."""

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
from pubg_brawl_direct import make_brawl_montage

ENV_FILE = Path("/root/.video_bot.env")
LOG = Path("/root/data/mlbb/pubg_tiktok_batch_10.log")
STATE_FILE = Path("/root/data/mlbb/pubg_tiktok_batch_10_state.json")

VARIANTS = [
    {"suffix": f"tt{i:02d}", "part": f"{i}/10"}
    for i in range(1, 11)
]
TOTAL_JOBS = len(VARIANTS)


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
        time.sleep(30)


def run_job(variant: dict, env: dict[str, str], state: dict) -> int:
    job_key = f"pubg:{variant['suffix']}"
    if job_is_ok(state, job_key):
        log(f"skip done {job_key}")
        return 0

    def attempt_fn(attempt: int) -> int:
        wait_editor()
        caption = (
            f"🔫 PUBG Metro {variant['part']} — замесы\n"
            f"Проверенные перестрелки, сцены не повторяются"
        )
        code, detail = make_brawl_montage(
            output_basename=f"pubg_tiktok_{variant['suffix']}",
            caption=caption,
            env=env,
        )
        if code == 0:
            mark_job(state, job_key, status="ok", path=STATE_FILE, output=detail, attempts=attempt)
            log(f"ok {variant['part']} -> {detail}")
            return 0
        log(f"fail {variant['part']} attempt {attempt}: {detail[:400]}")
        mark_job(state, job_key, status="retrying", path=STATE_FILE, error=detail, attempts=attempt)
        return code

    code = run_until_success(job_key, attempt_fn, max_attempts=5, log=log)
    if code == 0:
        return 0
    mark_job(state, job_key, status="failed", path=STATE_FILE)
    return code


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--reset", action="store_true")
    args = parser.parse_args()

    env = load_env()
    if not env.get("TG_BOT_TOKEN") or not env.get("TG_CHAT_ID"):
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

    log(f"pubg brawl batch — {count_ok_jobs(state)}/{TOTAL_JOBS} done")
    failures = 0
    for variant in VARIANTS:
        if run_job(variant, env, state) != 0:
            failures += 1
        time.sleep(5)

    ok = count_ok_jobs(state)
    if ok >= TOTAL_JOBS:
        state["completed"] = True
        state["completed_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        save_json_state(STATE_FILE, state)
        log(f"done all {TOTAL_JOBS}")
        return 0

    save_json_state(STATE_FILE, state)
    log(f"incomplete {ok}/{TOTAL_JOBS}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
