#!/usr/bin/env python3
"""
PUBG + Standoff morning catch-up: 2 montages each, auto-retry, deadline 08:00 MSK.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

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

MSK = ZoneInfo("Europe/Moscow")
ENV_FILE = Path("/root/.video_bot.env")
PROCESSOR = Path("/usr/local/bin/smart_video_editor.py")
INBOX = Path("/root/data/mlbb/youtube_nightly/inbox")
LOG = Path("/root/data/mlbb/morning_pubg_standoff.log")
STATE_FILE = Path("/root/data/mlbb/morning_pubg_standoff_state.json")

GAMES = [
    {
        "id": "pubg",
        "profile": "pubg",
        "label": "PUBG Metro",
        "sources": [
            "yt_n97cHIR9Qow.mp4",
            "yt_FpMs48XOnq0.mp4",
            "yt_zv3JymSZOb0.mp4",
        ],
    },
    {
        "id": "standoff",
        "profile": "standoff",
        "label": "Standoff 2",
        "sources": [
            "yt_z8ImUR0_x_M.mp4",
        ],
        "fallback_urls": [
            "https://www.youtube.com/live/0pneRQba1rE",
        ],
    },
]

VARIANTS = [
    {"suffix": "v1", "part": "1/2", "SELECTION_VARIANT": "0", "extra": {}},
    {
        "suffix": "v2",
        "part": "2/2",
        "SELECTION_VARIANT": "2",
        "extra": {
            "SMART_PUBG_PEAK_PERCENTILE": "30",
            "SMART_STANDOFF_PEAK_PERCENTILE": "24",
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


def deadline_reached(hour_msk: int = 8) -> bool:
    now = datetime.now(MSK)
    stop = now.replace(hour=hour_msk, minute=5, second=0, microsecond=0)
    return now >= stop


def wait_editor() -> None:
    while subprocess.run(["pgrep", "-f", "smart_video_editor.py"], capture_output=True).returncode == 0:
        time.sleep(40)


def ensure_standoff_source(game: dict, env: dict[str, str], attempt: int) -> Path | None:
    for name in game["sources"]:
        path = INBOX / name
        if path.exists():
            return path
    if attempt >= 2 and game.get("fallback_urls"):
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        try:
            from youtube_download import download_video
        except ImportError:
            return None
        for url in game["fallback_urls"]:
            try:
                log(f"standoff download fallback {url}")
                path = download_video(url, env)
                if path and path.exists():
                    dest = INBOX / f"yt_{path.stem}.mp4"
                    if path != dest:
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        if dest.exists():
                            dest.unlink()
                        path.replace(dest)
                    return dest
            except Exception as exc:
                log(f"standoff download fail: {exc}")
    return None


def resolve_source(game: dict, attempt: int, env: dict[str, str]) -> Path | None:
    if game["id"] == "standoff":
        return ensure_standoff_source(game, env, attempt)
    names = game["sources"]
    index = min(attempt - 1, len(names) - 1)
    for name in names[index:] + names[:index]:
        path = INBOX / name
        if path.exists():
            return path
    return None


def run_attempt(
    source: Path,
    game: dict,
    variant: dict,
    env: dict[str, str],
    chat_id: str,
    attempt: int,
) -> tuple[int, str]:
    from montage_env import profile_montage_env, relaxed_montage_env

    profile = game["profile"]
    gid = game["id"]
    with tempfile.NamedTemporaryFile("w", delete=False, suffix=".queue.txt") as tmp:
        tmp.write(f"{source.resolve()}|{game['label']}|{chat_id}\n")
        queue_path = tmp.name

    history = Path(f"/tmp/morning_{gid}_history.json")
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
            "OUTPUT_BASENAME": f"morning_{gid}_{variant['suffix']}",
            "MONTAGE_CAPTION": (
                f"⚔️ {game['label']} {variant['part']} — утренняя нарезка\n"
                f"Перестрелки / дуэли, без пустого бега"
            ),
            "SMART_ALLOW_EXCLUDED_FALLBACK": "0",
            "PIPELINE_JOB_RETRIES": "6",
        }
    )
    if attempt < 3:
        run_env.setdefault("OVERNIGHT_FRESH_SEGMENTS", "1")

    slug = f"morning_{gid}_{variant['suffix']}"
    out_dir = Path(run_env["OUTPUT_DIR"])
    before = {p.name for p in out_dir.glob(f"{slug}_*.mp4")}

    try:
        log(f"start {game['label']} {variant['part']} ({source.name}) attempt={attempt}")
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
        log(f"ok {game['label']} {variant['part']} -> {new_files[-1].name}")
        return 0, new_files[-1].name
    finally:
        Path(queue_path).unlink(missing_ok=True)


def run_job(game: dict, variant: dict, env: dict[str, str], chat_id: str, state: dict) -> int:
    job_key = f"{game['id']}:{variant['suffix']}"
    if job_is_ok(state, job_key):
        log(f"skip done {job_key}")
        return 0

    def attempt_fn(attempt: int) -> int:
        if deadline_reached():
            log(f"deadline 08:05 MSK reached, stop {job_key}")
            return 4
        wait_editor()
        source = resolve_source(game, attempt, env)
        if source is None:
            return 2
        code, detail = run_attempt(source, game, variant, env, chat_id, attempt)
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

    log(f"morning pubg+standoff — {count_ok_jobs(state)}/{TOTAL_JOBS} done")
    wait_editor()

    failures = 0
    for game in GAMES:
        if deadline_reached():
            break
        for variant in VARIANTS:
            if deadline_reached():
                break
            code = run_job(game, variant, env, chat_id, state)
            if code != 0:
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
    log(f"incomplete {ok}/{TOTAL_JOBS} failures={failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
