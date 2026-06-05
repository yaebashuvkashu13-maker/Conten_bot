#!/usr/bin/env python3
"""
One-off Genshin montage: boss fights only, skip previously used bad segments.
Tries alternate inbox VOD first, then primary source. Auto-retries on failure.
"""

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
LOG = Path("/root/data/mlbb/genshin_boss_rebuild.log")
STATE_FILE = Path("/root/data/mlbb/genshin_boss_rebuild_state.json")
JOB_KEY = "genshin:rebuild"

SOURCES = [
    INBOX / "yt_ViQhjTOShrA.mp4",
    INBOX / "yt_NXJuHTKXs2g.mp4",
]

BAD_START_SEC = {1038.0, 1418.0, 1436.0, 2038.0, 5072.0, 8188.0}

HISTORY_PATHS = [
    Path("/tmp/action_showcase_2x5_history/genshin.json"),
    Path("/root/.smart_edit_segment_history.json"),
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


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def collect_excluded_segments(source: Path) -> set[str]:
    excluded: set[str] = set()
    source_sig = file_sha256(source)
    for start in BAD_START_SEC:
        excluded.add(f"{source_sig}:{round(start, 3)}")
    for hist_path in HISTORY_PATHS:
        if not hist_path.exists():
            continue
        try:
            payload = json.loads(hist_path.read_text())
        except Exception:
            continue
        for key in payload.get("segment_keys", []):
            excluded.add(str(key))
        for sig in payload.get("source_signatures", []):
            for start in BAD_START_SEC:
                excluded.add(f"{sig}:{round(start, 3)}")
    return excluded


def wait_editor() -> None:
    while subprocess.run(["pgrep", "-f", "smart_video_editor.py"], capture_output=True).returncode == 0:
        time.sleep(30)


def pick_source(attempt: int) -> Path | None:
    available = [src for src in SOURCES if src.exists()]
    if not available:
        return None
    index = min(attempt - 1, len(available) - 1)
    return available[index]


def run_rebuild_attempt(source: Path, env: dict[str, str], chat_id: str, attempt: int) -> tuple[int, str]:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from montage_env import profile_montage_env, relaxed_montage_env

    excluded = collect_excluded_segments(source)
    with tempfile.NamedTemporaryFile("w", delete=False, suffix=".queue.txt") as tmp:
        tmp.write(f"{source.resolve()}|Genshin|{chat_id}\n")
        queue_path = tmp.name

    history = Path("/tmp/genshin_boss_rebuild_history.json")
    run_env = os.environ.copy()
    for key, value in env.items():
        run_env.setdefault(key, value)
    run_env.update(profile_montage_env("genshin"))
    if attempt >= 2:
        run_env.update(relaxed_montage_env("genshin"))
    if attempt >= 3:
        run_env["OVERNIGHT_FRESH_SEGMENTS"] = "1"
        run_env["SELECTION_VARIANT"] = str(attempt % 4)
    run_env.update(
        {
            "QUEUE_FILE": queue_path,
            "MAX_SOURCES": "1",
            "SINGLE_SOURCE_MODE": "1",
            "SEND_TELEGRAM": "1",
            "SMART_BLOCKING_LOCK": "1",
            "OUTPUT_DIR": "/root/videos",
            "DEFAULT_GAME_PROFILE": "genshin",
            "QUEUE_GAME_PROFILE": "genshin",
            "OVERNIGHT_FRESH_SEGMENTS": run_env.get("OVERNIGHT_FRESH_SEGMENTS", "0"),
            "SEGMENT_HISTORY_FILE": str(history),
            "SELECTION_VARIANT": run_env.get("SELECTION_VARIANT", "1"),
            "SMART_ALLOW_EXCLUDED_FALLBACK": "0",
            "EXCLUDED_SEGMENT_KEYS": ",".join(sorted(excluded)),
            "OUTPUT_BASENAME": "genshin_boss_rebuild",
            "MONTAGE_CAPTION": (
                "⚔️ Genshin — только боссы (пересборка)\n"
                "Бои с боссами, без крипов и простоя"
            ),
            "SMART_GENSHIN_MIN_BOSS_BAR": "0.20",
            "SMART_GENSHIN_MIN_BOSS_BAR_PEAK": "0.28",
            "SMART_GENSHIN_MIN_CENTER_MOTION": "0.020",
            "SMART_GENSHIN_MIN_AUDIO_RMS": "0.012",
            "SMART_GENSHIN_MIN_CLUSTER_SEC": "18",
            "SMART_GENSHIN_MIN_SEGMENT_GAP": "100",
        }
    )

    slug = "genshin_boss_rebuild"
    out_dir = Path(run_env["OUTPUT_DIR"])
    before = {p.name for p in out_dir.glob(f"{slug}_*.mp4")}

    try:
        log(f"start rebuild source={source.name} excluded={len(excluded)} attempt={attempt}")
        completed = subprocess.run(
            [sys.executable, str(PROCESSOR)],
            env=run_env,
            capture_output=True,
            text=True,
            timeout=int(float(env.get("SMART_MAKE_TIMEOUT_MAX_SEC", "14400"))),
        )
        tail = (completed.stderr or completed.stdout or "")[-1200:]
        new_files = [p for p in out_dir.glob(f"{slug}_*.mp4") if p.name not in before]
        if completed.returncode != 0:
            return completed.returncode, tail
        if not new_files:
            return 3, tail
        log(f"ok -> {new_files[-1].name}")
        return 0, new_files[-1].name
    finally:
        Path(queue_path).unlink(missing_ok=True)


def init_state(reset: bool) -> dict:
    if reset and STATE_FILE.exists():
        STATE_FILE.unlink(missing_ok=True)
    state = load_json_state(STATE_FILE)
    state.setdefault("jobs", {})
    state["total_jobs"] = 1
    state["started_at"] = state.get("started_at") or time.strftime("%Y-%m-%d %H:%M:%S")
    state["completed"] = False
    save_json_state(STATE_FILE, state)
    return state


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

    state = init_state(reset=args.reset)
    if pipeline_complete(state, 1) or job_is_ok(state, JOB_KEY):
        log("genshin rebuild already done")
        return 0

    def attempt_fn(attempt: int) -> int:
        wait_editor()
        source = pick_source(attempt)
        if source is None:
            log("no source files in inbox")
            return 2
        code, detail = run_rebuild_attempt(source, env, chat_id, attempt)
        if code == 0:
            mark_job(
                state,
                JOB_KEY,
                status="ok",
                path=STATE_FILE,
                output=detail,
                attempts=attempt,
            )
            state["completed"] = True
            state["completed_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            save_json_state(STATE_FILE, state)
            return 0
        mark_job(
            state,
            JOB_KEY,
            status="retrying",
            path=STATE_FILE,
            error=detail,
            attempts=attempt,
        )
        return code

    code = run_until_success(JOB_KEY, attempt_fn, log=log)
    if code == 0:
        return 0
    mark_job(state, JOB_KEY, status="failed", path=STATE_FILE)
    save_json_state(STATE_FILE, state)
    log("genshin rebuild failed — watchdog will retry")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
