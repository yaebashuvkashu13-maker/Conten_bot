#!/usr/bin/env python3
"""MLBB YouTube showcase: 2 action montages, auto-retry, Telegram delivery."""

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
    run_until_success,
    save_json_state,
)

ENV_FILE = Path("/root/.video_bot.env")
PROCESSOR = Path("/usr/local/bin/smart_video_editor.py")
INBOX = Path("/root/data/mlbb/youtube_nightly/inbox")
LOG = Path("/root/data/mlbb/mlbb_showcase_rebuild.log")
STATE_FILE = Path("/root/data/mlbb/mlbb_showcase_rebuild_state.json")
HISTORY = Path("/tmp/mlbb_showcase_rebuild_history.json")

SOURCES = [
    INBOX / "yt_2XbUY9dvS7Y.mp4",
    INBOX / "yt_559CEnq-8-o.mp4",
]

VARIANTS = [
    {"suffix": "v1", "part": "1/2", "SELECTION_VARIANT": "0", "extra": {}},
    {
        "suffix": "v2",
        "part": "2/2",
        "SELECTION_VARIANT": "2",
        "extra": {"SMART_MLBB_PEAK_PERCENTILE": "46"},
    },
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
        time.sleep(40)


def pick_source(attempt: int) -> Path | None:
    available = [s for s in SOURCES if s.exists()]
    if not available:
        return None
    return available[min(attempt - 1, len(available) - 1)]


def run_attempt(
    source: Path,
    variant: dict,
    env: dict[str, str],
    chat_id: str,
    attempt: int,
) -> tuple[int, str]:
    from montage_env import profile_montage_env, relaxed_montage_env

    with tempfile.NamedTemporaryFile("w", delete=False, suffix=".queue.txt") as tmp:
        tmp.write(f"{source.resolve()}|MLBB|{chat_id}\n")
        queue_path = tmp.name

    run_env = os.environ.copy()
    for key, value in env.items():
        run_env.setdefault(key, value)
    run_env.update(profile_montage_env("mobile_legends"))
    run_env.update(variant.get("extra") or {})
    if attempt >= 2:
        run_env.update(relaxed_montage_env("mobile_legends"))
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
            "DEFAULT_GAME_PROFILE": "mobile_legends",
            "QUEUE_GAME_PROFILE": "mobile_legends",
            "SEGMENT_HISTORY_FILE": str(HISTORY),
            "SELECTION_VARIANT": run_env.get("SELECTION_VARIANT", str(variant["SELECTION_VARIANT"])),
            "OUTPUT_BASENAME": f"mlbb_showcase_{variant['suffix']}",
            "STRICT_GAMEPLAY": "1",
            "SMART_ALLOW_EXCLUDED_FALLBACK": "0",
            "TARGET_DURATION": "45",
            "MIN_FINAL_DURATION": "40",
            "MAX_FINAL_DURATION": "55",
            "MONTAGE_CAPTION": (
                f"⚔️ MLBB {variant['part']} — только файты\n"
                f"Тимфайты / скиллы, без драфта и рекламы"
            ),
        }
    )
    if attempt < 3:
        run_env.setdefault("OVERNIGHT_FRESH_SEGMENTS", "1")

    slug = f"mlbb_showcase_{variant['suffix']}"
    out_dir = Path(run_env["OUTPUT_DIR"])
    before = {p.name for p in out_dir.glob(f"{slug}_*.mp4")}

    try:
        log(f"start MLBB {variant['part']} ({source.name}) attempt={attempt}")
        completed = subprocess.run(
            [sys.executable, str(PROCESSOR)],
            env=run_env,
            capture_output=True,
            text=True,
            timeout=int(float(env.get("SMART_MAKE_TIMEOUT_MAX_SEC", "14400"))),
        )
        tail = (completed.stderr or completed.stdout or "")[-900:]
        new_files = [p for p in out_dir.glob(f"{slug}_*.mp4") if p.name not in before]
        if completed.returncode != 0:
            return completed.returncode, tail
        if not new_files:
            return 3, tail
        log(f"ok -> {new_files[-1].name}")
        return 0, new_files[-1].name
    finally:
        Path(queue_path).unlink(missing_ok=True)


def run_job(variant: dict, env: dict[str, str], chat_id: str, state: dict) -> int:
    job_key = f"mlbb:{variant['suffix']}"
    if job_is_ok(state, job_key):
        log(f"skip done {job_key}")
        return 0

    def attempt_fn(attempt: int) -> int:
        wait_editor()
        source = pick_source(attempt)
        if source is None:
            return 2
        code, detail = run_attempt(source, variant, env, chat_id, attempt)
        if code == 0:
            mark_job(state, job_key, status="ok", path=STATE_FILE, output=detail, attempts=attempt)
            return 0
        mark_job(state, job_key, status="retrying", path=STATE_FILE, error=detail, attempts=attempt)
        return code

    code = run_until_success(job_key, attempt_fn, max_attempts=6, log=log)
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

    log(f"mlbb showcase rebuild — {count_ok_jobs(state)}/{TOTAL_JOBS} done")
    wait_editor()

    failures = 0
    for variant in VARIANTS:
        if run_job(variant, env, chat_id, state) != 0:
            failures += 1
        time.sleep(6)

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
