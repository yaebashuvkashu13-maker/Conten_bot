#!/usr/bin/env python3
"""One sequential MLBB montage from YouTube Shorts for a single hero (pilot)."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from gameplay_gate import path_blocked_by_calibration, source_has_valid_gameplay_window
from mlbb_calibration_store import SHORTS_ROOT, labeled_ids, load_index, rebuild_index_from_disk
from mlbb_hero_dataset_builder import hero_from_text, load_heroes

HERO_ROOT = Path("/root/hero_datasets")
OUTPUT_DIR = Path("/root/videos")
STATE_PATH = Path(os.environ.get("MLBB_HERO_SHORTS_MONTAGE_STATE", "/root/data/mlbb/hero_shorts_montage.json"))
MIN_SOURCES = int(os.environ.get("MLBB_HERO_MONTAGE_MIN_SOURCES", "4"))
MAX_SOURCES = int(os.environ.get("MLBB_HERO_MONTAGE_MAX_SOURCES", "8"))
INGEST_IF_BELOW = int(os.environ.get("MLBB_HERO_MONTAGE_INGEST_IF_BELOW", "6"))

# Heroes often in Shorts titles but missing from the main registry.
EXTRA_HERO_TAGS: dict[str, list[str]] = {
    "dyrroth": ["dyrroth"],
    "harith": ["harith"],
    "hayabusa": ["haya", "hayabusa"],
    "wu_zetian": ["wu zetian", "zetian"],
}


def _hero_match(text: str, heroes: list[dict]) -> str | None:
    hid = hero_from_text(text, heroes)
    if hid:
        return hid
    low = text.lower()
    for hero_id, tags in EXTRA_HERO_TAGS.items():
        for tag in tags:
            if re.search(rf"\b{re.escape(tag.lower())}\b", low):
                return hero_id
    return None


def _views_per_day(row: dict) -> float:
    views = int(row.get("view_count") or 0)
    upload = str(row.get("upload_date") or "")
    if len(upload) == 8 and upload.isdigit():
        try:
            uploaded = datetime.strptime(upload, "%Y%m%d").replace(tzinfo=timezone.utc)
            age = max(1.0, (datetime.now(timezone.utc) - uploaded).total_seconds() / 86400.0)
            return views / age
        except ValueError:
            pass
    return float(views)


def pick_popular_hero(*, forced: str = "") -> tuple[str, dict[str, float]]:
    heroes = load_heroes()
    rebuild_index_from_disk()
    totals: dict[str, float] = {}
    counts: dict[str, int] = {}
    for row in load_index().get("candidates", []):
        path = Path(str(row.get("path", "")))
        if not path.exists():
            path = SHORTS_ROOT / f"yt_{row.get('video_id', '')}.mp4"
        if not path.exists():
            continue
        text = f"{row.get('title', '')} {row.get('search_query', '')} {row.get('url', '')}"
        hid = _hero_match(text, heroes)
        if not hid:
            continue
        vpd = _views_per_day(row)
        totals[hid] = totals.get(hid, 0.0) + vpd
        counts[hid] = counts.get(hid, 0) + 1
    if forced:
        hid = forced.strip().lower()
        return hid, totals
    if not totals:
        return "", totals
    best = max(totals.items(), key=lambda item: item[1])[0]
    return best, totals


def collect_shorts_for_hero(hero_id: str, *, limit: int) -> list[dict]:
    heroes = load_heroes()
    labeled = labeled_ids()
    used = set()
    if STATE_PATH.exists():
        try:
            used = set(json.loads(STATE_PATH.read_text()).get("used_video_ids", []))
        except (json.JSONDecodeError, OSError):
            used = set()

    rows: list[dict] = []
    for row in load_index().get("candidates", []):
        vid = str(row.get("video_id", ""))
        if not vid or vid in used:
            continue
        path = Path(str(row.get("path", "")))
        if not path.exists():
            path = SHORTS_ROOT / f"yt_{vid}.mp4"
        if not path.exists():
            continue
        text = f"{row.get('title', '')} {row.get('search_query', '')}"
        if _hero_match(text, heroes) != hero_id:
            continue
        if path_blocked_by_calibration(path):
            continue
        rank = _views_per_day(row)
        if labeled.get(vid) == "good":
            rank += 1000.0
        elif labeled.get(vid) == "bad":
            continue
        rows.append({**row, "path": str(path), "rank": rank})
    rows.sort(key=lambda r: float(r.get("rank") or 0), reverse=True)
    return rows[:limit]


def ingest_hero_shorts(hero_id: str, *, count: int) -> int:
    ingest = Path("/usr/local/bin/mlbb_youtube_shorts_ingest.py")
    if not ingest.exists():
        ingest = Path(__file__).resolve().parent / "mlbb_youtube_shorts_ingest.py"
    query = f"mlbb {hero_id.replace('_', ' ')} savage shorts"
    env = dict(os.environ)
    env["MLBB_INGEST_SKIP_IF_PENDING"] = "0"
    env["MLBB_CALIBRATION_FAST_INGEST"] = "1"
    proc = subprocess.run(
        [
            sys.executable,
            str(ingest),
            "--incremental",
            "--days",
            env.get("MLBB_SHORTS_DAYS", "365"),
            "--max-downloads",
            str(count),
            "--max-per-query",
            "16",
        ],
        env=env,
        timeout=900,
        check=False,
    )
    rebuild_index_from_disk()
    return proc.returncode


def stage_sources(hero_id: str, rows: list[dict]) -> list[Path]:
    dest_dir = HERO_ROOT / hero_id / "from_shorts"
    dest_dir.mkdir(parents=True, exist_ok=True)
    out: list[Path] = []
    for row in rows:
        src = Path(row["path"])
        vid = str(row.get("video_id", ""))
        dest = dest_dir / f"shorts_yt_{vid}.mp4"
        if dest.exists() or dest.is_symlink():
            dest.unlink(missing_ok=True)
        dest.symlink_to(src.resolve())
        out.append(dest)
    return out


def build_queue(paths: list[Path], chat_id: str, hero_id: str, theme: str) -> Path:
    fd, queue_path = tempfile.mkstemp(prefix="hero-shorts-", suffix=".txt", dir="/tmp")
    label = f"{theme} | {hero_id.replace('_', ' ').title()}"
    with os.fdopen(fd, "w") as handle:
        for path in paths:
            handle.write(f"{path}|{label}|{chat_id}\n")
    return Path(queue_path)


def run_montage(hero_id: str, sources: list[Path], chat_id: str, theme: str) -> int:
    queue_path = build_queue(sources, chat_id, hero_id, theme)
    env = os.environ.copy()
    env.update(
        {
            "QUEUE_FILE": str(queue_path),
            "MAX_SOURCES": str(len(sources)),
            "OUTPUT_DIR": str(OUTPUT_DIR),
            "OUTPUT_BASENAME": f"mlbb_shorts_{hero_id}_{int(time.time())}",
            "SINGLE_HERO_MODE": "1",
            "SINGLE_HERO_ID": hero_id,
            "SEND_TELEGRAM": "1",
            "STRICT_GAMEPLAY": "1",
            "SMART_REQUIRE_UNIFORM_GAMEPLAY": "1",
            "SMART_REJECT_HERO_SHOWCASE": "1",
            "TARGET_DURATION": "42",
            "MIN_FINAL_DURATION": "30",
            "MAX_FINAL_DURATION": "55",
            "MIN_HIGHLIGHTS": "3",
            "MAX_HIGHLIGHTS": "4",
            "SMART_ADD_MUSIC": "0",
            "SMART_GAME_AUDIO_ONLY": "1",
            "SMART_STRIP_MUSIC_BED": "1",
            "BLUR_NICKNAME": "0",
            "SMART_MIN_HUD": "15",
            "SMART_MIN_HUD_FRAME_RATE": "0.60",
            "SMART_UNIFORM_MIN_HUD_RATE": "0.65",
            "SMART_MIN_CENTER_MOTION": "0.015",
            "SMART_MAX_CHAT_PANEL": "0.18",
            "SMART_MAX_CENTER_TEXT": "0.14",
            "SMART_MAX_OVERLAY_TEXT": "0.58",
            "SMART_REJECT_PROMO": "1",
            "SELECTION_VARIANT": str(int(time.time()) % 5),
            "MLBB_ONE_HEAVY_JOB": "1",
        }
    )
    editor = Path("/usr/local/bin/smart_video_editor.py")
    if not editor.exists():
        editor = Path(__file__).resolve().parent / "smart_video_editor.py"
    try:
        return subprocess.run([sys.executable, str(editor)], env=env, check=False, timeout=3600).returncode
    finally:
        queue_path.unlink(missing_ok=True)


def save_state(hero_id: str, video_ids: list[str], *, rc: int) -> None:
    state: dict = {"used_video_ids": [], "runs": []}
    if STATE_PATH.exists():
        try:
            state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            state = {"used_video_ids": [], "runs": []}
    used = set(state.get("used_video_ids", []))
    used.update(video_ids)
    state["used_video_ids"] = sorted(used)
    state.setdefault("runs", []).append(
        {
            "hero": hero_id,
            "video_ids": video_ids,
            "rc": rc,
            "at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
    )
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--hero", default=os.environ.get("MLBB_HERO_MONTAGE_HERO", ""))
    parser.add_argument("--ingest", type=int, default=INGEST_IF_BELOW)
    args = parser.parse_args()

    chat_id = os.environ.get("TG_CHAT_ID", "")
    if not chat_id:
        print("TG_CHAT_ID missing", file=sys.stderr)
        return 1

    hero_id, totals = pick_popular_hero(forced=args.hero)
    if not hero_id:
        print("no hero found in shorts index titles", file=sys.stderr)
        return 1
    print(f"hero={hero_id} views_score={totals.get(hero_id, 0):.1f} totals={totals}")

    rows = collect_shorts_for_hero(hero_id, limit=MAX_SOURCES)
    if len(rows) < MIN_SOURCES and args.ingest > 0:
        print(f"ingest extra hero shorts need={MIN_SOURCES} have={len(rows)}")
        ingest_hero_shorts(hero_id, count=args.ingest)
        rows = collect_shorts_for_hero(hero_id, limit=MAX_SOURCES)

    playable: list[dict] = []
    for row in rows:
        path = Path(row["path"])
        ok, _ = source_has_valid_gameplay_window(path, windows=2, window_sec=8.0)
        if ok:
            playable.append(row)
    if len(playable) < MIN_SOURCES:
        print(f"not enough playable shorts hero={hero_id} have={len(playable)} need={MIN_SOURCES}")
        return 1

    sources = stage_sources(hero_id, playable[:MAX_SOURCES])
    theme = "Shorts montage pilot"
    print(f"montage hero={hero_id} sources={len(sources)}")
    rc = run_montage(hero_id, sources, chat_id, theme)
    save_state(hero_id, [str(r.get("video_id", "")) for r in playable[:MAX_SOURCES]], rc=rc)
    print(f"done rc={rc}")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
