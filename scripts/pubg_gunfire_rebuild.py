#!/usr/bin/env python3
"""PUBG Metro rebuild: gunfire-only scenes, exclude walk/talk segments."""

from __future__ import annotations

import argparse
import hashlib
import json
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
LOG = Path("/root/data/mlbb/pubg_gunfire_rebuild.log")
STATE_FILE = Path("/root/data/mlbb/pubg_gunfire_rebuild_state.json")

SOURCES = [
    INBOX / "yt_n97cHIR9Qow.mp4",
    INBOX / "yt_FpMs48XOnq0.mp4",
]

BAD_START_SEC = {
    130.4, 146.4, 356.4, 590.4, 612.4, 824.4, 992.4, 1418.4, 2038.4, 2105.4,
    2178.4, 2215.4, 2287.4, 2521.4, 3432.4, 3598.4, 3718.4, 4124.4, 5072.4,
    5416.4, 5464.4, 5820.4, 6158.4, 6256.4, 6652.4, 7598.4, 7764.4, 7904.4,
    8120.4, 8126.4, 8218.4, 8630.4, 9110.4, 9124.4, 9190.4, 9226.4, 9246.4,
    9558.4, 9718.4, 9936.4, 10006.4,
}

VARIANTS = [
    {"suffix": "v1", "part": "1/2", "SELECTION_VARIANT": "0", "extra": {}},
    {
        "suffix": "v2",
        "part": "2/2",
        "SELECTION_VARIANT": "2",
        "extra": {"SMART_PUBG_PEAK_PERCENTILE": "30", "SMART_PUBG_GUNFIRE_PERCENTILE": "52"},
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


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def collect_excluded(source: Path) -> set[str]:
    excluded: set[str] = set()
    sig = file_sha256(source)
    for start in BAD_START_SEC:
        excluded.add(f"{sig}:{round(start, 3)}")
    for hist in (
        Path("/tmp/morning_pubg_history.json"),
        Path("/root/.smart_edit_segment_history.json"),
        Path("/tmp/action_showcase_2x5_history/pubg.json"),
    ):
        if not hist.exists():
            continue
        try:
            payload = json.loads(hist.read_text())
        except Exception:
            continue
        for key in payload.get("segment_keys", []):
            excluded.add(str(key))
    return excluded


def wait_editor() -> None:
    while subprocess.run(["pgrep", "-f", "smart_video_editor.py"], capture_output=True).returncode == 0:
        time.sleep(35)


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

    excluded = collect_excluded(source)
    with tempfile.NamedTemporaryFile("w", delete=False, suffix=".queue.txt") as tmp:
        tmp.write(f"{source.resolve()}|PUBG Metro|{chat_id}\n")
        queue_path = tmp.name

    history = Path("/tmp/pubg_gunfire_rebuild_history.json")
    run_env = os.environ.copy()
    for key, value in env.items():
        run_env.setdefault(key, value)
    run_env.update(profile_montage_env("pubg"))
    run_env.update(variant.get("extra") or {})
    if attempt >= 2:
        run_env.update(relaxed_montage_env("pubg"))
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
            "DEFAULT_GAME_PROFILE": "pubg",
            "QUEUE_GAME_PROFILE": "pubg",
            "SEGMENT_HISTORY_FILE": str(history),
            "SELECTION_VARIANT": run_env.get("SELECTION_VARIANT", str(variant["SELECTION_VARIANT"])),
            "OUTPUT_BASENAME": f"pubg_gunfire_{variant['suffix']}",
            "EXCLUDED_SEGMENT_KEYS": ",".join(sorted(excluded)),
            "SMART_ALLOW_EXCLUDED_FALLBACK": "0",
            "MONTAGE_CAPTION": (
                f"🔫 PUBG Metro {variant['part']} — только перестрелки\n"
                f"Стрельба / бои, без бега и разговоров стримера"
            ),
        }
    )
    if attempt < 3:
        run_env.setdefault("OVERNIGHT_FRESH_SEGMENTS", "1")

    slug = f"pubg_gunfire_{variant['suffix']}"
    out_dir = Path(run_env["OUTPUT_DIR"])
    before = {p.name for p in out_dir.glob(f"{slug}_*.mp4")}

    try:
        log(f"start PUBG {variant['part']} ({source.name}) attempt={attempt}")
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
    job_key = f"pubg:{variant['suffix']}"
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

    log(f"pubg gunfire rebuild — {count_ok_jobs(state)}/{TOTAL_JOBS} done")
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
