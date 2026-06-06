#!/usr/bin/env python3
"""Ten diverse PUBG Metro TikTok montages (~45s, 33–57s), owner-calibrated."""

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
LOG = Path("/root/data/mlbb/pubg_tiktok_batch_10.log")
STATE_FILE = Path("/root/data/mlbb/pubg_tiktok_batch_10_state.json")
HISTORY_FILE = Path("/tmp/pubg_tiktok_batch_10_history.json")
OUT_DIR = Path("/root/videos")

# Calibrated VOD only — other streams pass audio without visible gunfights.
SOURCE_NAMES = [
    "yt_n97cHIR9Qow.mp4",
]

BAD_START_SEC = {
    130.4, 146.4, 356.4, 590.4, 612.4, 824.4, 992.4, 1418.4, 2038.4, 2050.4,
    2105.4, 2164.4, 2178.4, 2215.4, 2287.4, 2521.4, 3432.4, 3598.4, 3718.4,
    4124.4, 5072.4, 5416.4, 5464.4, 5672.4, 5820.4, 6158.4, 6256.4, 6652.4,
    7598.4, 7764.4, 7904.4, 8060.4, 8120.4, 8126.4, 8218.4, 8630.4, 9110.4,
    9124.4, 9190.4, 9226.4, 9246.4, 9558.4, 9718.4, 9936.4, 10006.4,
}

PEAK_CYCLE = [42, 36, 30, 46, 34, 40, 28, 44, 32, 38]
GUN_CYCLE = [58, 52, 46, 60, 50, 54, 44, 62, 48, 56]
SUSTAIN_CYCLE = [30, 26, 22, 32, 24, 28, 20, 34, 22, 27]

VARIANTS = [
    {
        "suffix": f"tt{i:02d}",
        "part": f"{i}/10",
        "SELECTION_VARIANT": str(i - 1),
        "source_idx": (i - 1) % len(SOURCE_NAMES),
        "extra": {
            "SMART_PUBG_PEAK_PERCENTILE": str(PEAK_CYCLE[i - 1]),
            "SMART_PUBG_GUNFIRE_PERCENTILE": str(GUN_CYCLE[i - 1]),
            "SMART_PUBG_SUSTAIN_PERCENTILE": str(SUSTAIN_CYCLE[i - 1]),
        },
    }
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


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def segment_keys_from_json(json_path: Path) -> set[str]:
    keys: set[str] = set()
    try:
        payload = json.loads(json_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return keys
    for seg in payload.get("selected_segments", []):
        sig = seg.get("source_signature", "")
        start = round(float(seg.get("start", 0)), 3)
        if sig:
            keys.add(f"{sig}:{start}")
    return keys


def collect_excluded(source: Path, state: dict) -> set[str]:
    excluded: set[str] = set(state.get("used_segment_keys", []))
    sig = file_sha256(source)
    for start in BAD_START_SEC:
        excluded.add(f"{sig}:{round(start, 3)}")
    for hist in (
        HISTORY_FILE,
        Path("/tmp/morning_pubg_history.json"),
        Path("/tmp/pubg_gunfire_rebuild_history.json"),
        Path("/root/.smart_edit_segment_history.json"),
    ):
        if not hist.exists():
            continue
        try:
            payload = json.loads(hist.read_text(encoding="utf-8"))
        except Exception:
            continue
        for key in payload.get("segment_keys", []):
            excluded.add(str(key))
    for json_path in OUT_DIR.glob("pubg_tiktok_tt*.json"):
        excluded |= segment_keys_from_json(json_path)
    for json_path in OUT_DIR.glob("pubg_gunfire_*.json"):
        excluded |= segment_keys_from_json(json_path)
    for json_path in OUT_DIR.glob("morning_pubg_*.json"):
        excluded |= segment_keys_from_json(json_path)
    return excluded


def wait_editor() -> None:
    while subprocess.run(["pgrep", "-f", "smart_video_editor.py"], capture_output=True).returncode == 0:
        time.sleep(35)


def resolve_source(variant: dict, attempt: int) -> Path | None:
    names = SOURCE_NAMES[:]
    start_idx = int(variant.get("source_idx", 0))
    rotated = names[start_idx:] + names[:start_idx]
    pick = rotated[min(attempt - 1, len(rotated) - 1)]
    path = INBOX / pick
    if path.exists():
        return path
    for name in names:
        candidate = INBOX / name
        if candidate.exists():
            return candidate
    return None


def run_attempt(
    source: Path,
    variant: dict,
    env: dict[str, str],
    chat_id: str,
    attempt: int,
    excluded: set[str],
    state: dict,
) -> tuple[int, str]:
    from montage_env import profile_montage_env, relaxed_montage_env

    with tempfile.NamedTemporaryFile("w", delete=False, suffix=".queue.txt") as tmp:
        tmp.write(f"{source.resolve()}|PUBG Metro|{chat_id}\n")
        queue_path = tmp.name

    run_env = os.environ.copy()
    for key, value in env.items():
        run_env.setdefault(key, value)
    run_env.update(profile_montage_env("pubg"))
    run_env.update(variant.get("extra") or {})
    if attempt >= 2:
        run_env.update(relaxed_montage_env("pubg"))
    if attempt >= 3:
        run_env["OVERNIGHT_FRESH_SEGMENTS"] = "1"
        run_env["SELECTION_VARIANT"] = str(
            (int(variant["SELECTION_VARIANT"]) + attempt * 3) % 12
        )
    run_env.update(
        {
            "QUEUE_FILE": queue_path,
            "MAX_SOURCES": "1",
            "SINGLE_SOURCE_MODE": "1",
            "SEND_TELEGRAM": "1",
            "SMART_BLOCKING_LOCK": "1",
            "OUTPUT_DIR": str(OUT_DIR),
            "DEFAULT_GAME_PROFILE": "pubg",
            "QUEUE_GAME_PROFILE": "pubg",
            "SEGMENT_HISTORY_FILE": str(HISTORY_FILE),
            "SELECTION_VARIANT": run_env.get(
                "SELECTION_VARIANT", str(variant["SELECTION_VARIANT"])
            ),
            "OUTPUT_BASENAME": f"pubg_tiktok_{variant['suffix']}",
            "EXCLUDED_SEGMENT_KEYS": ",".join(sorted(excluded)),
            "SMART_ALLOW_EXCLUDED_FALLBACK": "0",
            "OVERNIGHT_FRESH_SEGMENTS": "1",
            "SMART_EXPLORE_WINDOW": "12",
            "SMART_PUBG_MIN_SEGMENT_GAP": "110",
            "SMART_PUBG_TIKTOK_COMBAT": "1",
            "SMART_PUBG_ANCHOR_GOOD_ONLY": "1",
            "SMART_PUBG_CLIP_MAX_SEC": "10.5",
            "SMART_ACTION_CLIP_MAX_SEC": "10.5",
            "TARGET_DURATION": "45",
            "MIN_FINAL_DURATION": "40",
            "MAX_FINAL_DURATION": "57",
            "MIN_HIGHLIGHTS": "5",
            "MAX_HIGHLIGHTS": "5",
            "MONTAGE_CAPTION": (
                f"🔫 PUBG Metro TikTok {variant['part']}\n"
                f"Только замесы у твоих меток (30:45 / 35:50 / 41:10)"
            ),
        }
    )

    slug = f"pubg_tiktok_{variant['suffix']}"
    before = {p.name for p in OUT_DIR.glob(f"{slug}_*.mp4")}

    try:
        log(f"start {variant['part']} ({source.name}) attempt={attempt}")
        completed = subprocess.run(
            [sys.executable, str(PROCESSOR)],
            env=run_env,
            capture_output=True,
            text=True,
            timeout=int(float(env.get("SMART_MAKE_TIMEOUT_MAX_SEC", "14400"))),
        )
        tail = (completed.stderr or completed.stdout or "")[-1200:]
        new_files = sorted(
            p for p in OUT_DIR.glob(f"{slug}_*.mp4") if p.name not in before
        )
        if completed.returncode != 0:
            return completed.returncode, tail
        if not new_files:
            return 3, tail
        out_name = new_files[-1].name
        json_path = OUT_DIR / out_name.replace(".mp4", ".json")
        if json_path.exists():
            new_keys = segment_keys_from_json(json_path)
            if new_keys:
                used = set(state.get("used_segment_keys", []))
                used |= new_keys
                state["used_segment_keys"] = sorted(used)
                save_json_state(STATE_FILE, state)
        log(f"ok {variant['part']} -> {out_name}")
        return 0, out_name
    finally:
        Path(queue_path).unlink(missing_ok=True)


def run_job(variant: dict, env: dict[str, str], chat_id: str, state: dict) -> int:
    job_key = f"pubg:{variant['suffix']}"
    if job_is_ok(state, job_key):
        log(f"skip done {job_key}")
        return 0

    def attempt_fn(attempt: int) -> int:
        wait_editor()
        source = resolve_source(variant, attempt)
        if source is None:
            return 2
        excluded = collect_excluded(source, state)
        code, detail = run_attempt(source, variant, env, chat_id, attempt, excluded, state)
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
    state.setdefault("used_segment_keys", [])
    state["total_jobs"] = TOTAL_JOBS
    state["completed"] = False
    save_json_state(STATE_FILE, state)

    if pipeline_complete(state, TOTAL_JOBS):
        log(f"already complete ({count_ok_jobs(state)}/{TOTAL_JOBS})")
        return 0

    log(f"pubg tiktok batch — {count_ok_jobs(state)}/{TOTAL_JOBS} done")
    wait_editor()

    failures = 0
    for variant in VARIANTS:
        if run_job(variant, env, chat_id, state) != 0:
            failures += 1
        time.sleep(8)

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
