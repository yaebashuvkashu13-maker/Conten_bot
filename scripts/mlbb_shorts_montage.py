#!/usr/bin/env python3
"""MLBB-only montages from short sources (TikTok, YouTube Shorts, Telegram uploads).

Short = 12–180 s. Multi-source batch → 33–57 s montage → auto sendVideo (no preview gate).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from montage_env import strict_peak_env
from source_freshness import filter_new_sources, mark_used

TIKTOK_ROOT = Path("/root/datasets/tiktok/mlbb")
TELEGRAM_PENDING = Path("/root/telegram_uploads/pending")
YT_SHORTS_INBOX = Path("/root/data/mlbb/youtube_shorts/inbox")
OUTPUT_DIR = Path("/root/videos")
PROCESSOR = Path("/usr/local/bin/smart_video_editor.py")
STATE_PATH = Path("/root/data/mlbb/mlbb_shorts_cycle.json")

SHORT_MIN_SEC = float(os.environ.get("MLBB_SHORT_MIN_SEC", "12"))
SHORT_MAX_SEC = float(os.environ.get("MLBB_SHORT_MAX_SEC", "180"))
SOURCE_MAX_AGE_HOURS = float(os.environ.get("SOURCE_MAX_AGE_HOURS", "36"))
MAX_SOURCES_GATHER = int(os.environ.get("MLBB_SHORTS_GATHER_MAX", "24"))
SOURCES_PER_MONTAGE = int(os.environ.get("MLBB_SHORTS_BATCH_SIZE", "4"))
MONTAGES_PER_CYCLE = int(os.environ.get("MLBB_SHORTS_PER_CYCLE", "3"))


def ffprobe_duration(path: Path) -> float:
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode == 0 and result.stdout.strip():
            return float(result.stdout.strip())
    except (ValueError, subprocess.TimeoutExpired, OSError):
        pass
    return 0.0


def is_short_source(path: Path) -> bool:
    if not path.exists() or path.suffix.lower() != ".mp4":
        return False
    dur = ffprobe_duration(path)
    return SHORT_MIN_SEC <= dur <= SHORT_MAX_SEC


def gather_candidate_paths() -> list[Path]:
    paths: list[Path] = []
    if TIKTOK_ROOT.exists():
        paths.extend(sorted(TIKTOK_ROOT.rglob("*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True))
    if TELEGRAM_PENDING.exists():
        for chat_dir in TELEGRAM_PENDING.iterdir():
            if chat_dir.is_dir():
                paths.extend(chat_dir.glob("*.mp4"))
    if YT_SHORTS_INBOX.exists():
        paths.extend(sorted(YT_SHORTS_INBOX.glob("*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True))
    return paths


def mlbb_shorts_env(chat_id: str, token: str) -> dict[str, str]:
    env = strict_peak_env("mobile_legends")
    env.update(
        {
            "MLBB_SHORTS_MODE": "1",
            "MLBB_SHORTS_AUTO_SEND": "1",
            "OWNER_PREVIEW_APPROVED": "1",
            "OWNER_PREVIEW_REQUIRED": "0",
            "STRICT_PEAK_MONTAGE": "1",
            "STRICT_GAMEPLAY": "1",
            "SEND_TELEGRAM": "0",
            "TG_CHAT_ID": chat_id,
            "TG_BOT_TOKEN": token,
            "OUTPUT_DIR": str(OUTPUT_DIR),
            "DEFAULT_GAME_PROFILE": "mobile_legends",
            "QUEUE_GAME_PROFILE": "mobile_legends",
            "MIN_HIGHLIGHTS": "3",
            "MAX_HIGHLIGHTS": "4",
            "MIN_FINAL_DURATION": "33",
            "MAX_FINAL_DURATION": "57",
            "SMART_SKIP_INTRO_SEC": "2",
            "SMART_ACTION_CLIP_MIN_SEC": "6",
            "SMART_ACTION_CLIP_MAX_SEC": "11",
            "SMART_MAKE_TIMEOUT_SEC": "600",
            "SMART_MAKE_TIMEOUT_MAX_SEC": "900",
            "SMART_GAME_AUDIO_ONLY": "0",
            "SMART_STRIP_MUSIC_BED": "0",
            "SMART_ADD_MUSIC": "0",
            "BLUR_NICKNAME": "0",
            "HIGHLIGHT_SCORER": "0",
        }
    )
    return env


def _chunk(items: list[Path], size: int) -> list[list[Path]]:
    if size <= 0:
        return [items] if items else []
    return [items[i : i + size] for i in range(0, len(items), size)]


def _latest_montage_meta(since_ts: float) -> tuple[Path | None, dict | None]:
    candidates = sorted(OUTPUT_DIR.glob("*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True)
    for mp4 in candidates:
        if mp4.stat().st_mtime < since_ts:
            break
        meta_path = mp4.with_suffix(".json")
        if not meta_path.exists():
            continue
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if meta.get("profile") in ("mobile_legends", "mlbb") or "Mobile Legends" in str(
            meta.get("selected_segments", [])
        ):
            return mp4, meta
    return None, None


def verify_and_send(
    montage_path: Path,
    meta: dict,
    *,
    chat_id: str,
    token: str,
    caption: str,
) -> tuple[bool, str]:
    from visual_action_check import extract_and_check_segment

    segments = meta.get("selected_segments") or []
    if len(segments) < 3:
        return False, "too_few_segments"

    profile = "mobile_legends"
    passed = 0
    for item in segments:
        vod = Path(item.get("source_path") or montage_path)
        start = float(item["start"])
        dur = float(item.get("input_duration") or item.get("output_duration") or 9.0)
        row = extract_and_check_segment(vod, start, dur, profile)
        if row["visual_pass"]:
            passed += 1

    if passed < len(segments):
        return False, f"visual_fail:{passed}/{len(segments)}"

    os.environ["OWNER_PREVIEW_APPROVED"] = "1"
    os.environ["MLBB_SHORTS_AUTO_SEND"] = "1"
    os.environ["QUEUE_GAME_PROFILE"] = "mobile_legends"
    os.environ["DEFAULT_GAME_PROFILE"] = "mobile_legends"
    os.environ["STRICT_PEAK_MONTAGE"] = "1"

    from smart_video_editor import send_telegram_video

    send_telegram_video(token, chat_id, montage_path, caption)
    return True, "sent"


def run_montage_batch(
    sources: list[Path],
    *,
    chat_id: str,
    token: str,
    label: str = "MLBB shorts",
) -> tuple[int, str]:
    if not sources:
        return 1, "no_sources"

    short_sources = [p for p in sources if is_short_source(p)]
    if not short_sources:
        long_names = [p.name for p in sources][:3]
        return 1, f"not_short_sources:{long_names}"
    if len(short_sources) == 1 and ffprobe_duration(short_sources[0]) < 33:
        return 1, "single_short_too_short_for_montage"

    started = time.time()
    with tempfile.NamedTemporaryFile("w", delete=False, prefix="mlbb-shorts-", suffix=".txt") as tmp:
        for path in short_sources:
            tmp.write(f"{path.resolve()}|{label}|{chat_id}\n")
        queue_path = tmp.name

    run_env = os.environ.copy()
    run_env.update(mlbb_shorts_env(chat_id, token))
    run_env["QUEUE_FILE"] = queue_path
    run_env["MAX_SOURCES"] = str(len(short_sources))
    if len(short_sources) == 1:
        run_env["SINGLE_SOURCE_MODE"] = "1"
        run_env["MIN_HIGHLIGHTS"] = "3"

    processor = PROCESSOR if PROCESSOR.exists() else Path(__file__).resolve().parent / "smart_video_editor.py"
    try:
        completed = subprocess.run(
            [sys.executable, str(processor)],
            env=run_env,
            capture_output=True,
            text=True,
            timeout=int(run_env["SMART_MAKE_TIMEOUT_MAX_SEC"]),
        )
        tail = (completed.stderr or completed.stdout or "")[-800:]
        if completed.returncode != 0:
            return completed.returncode, f"editor_failed:{tail}"

        montage_path, meta = _latest_montage_meta(started)
        if not montage_path or not meta:
            return 1, f"no_output:{tail}"

        duration = float(meta.get("final_duration") or ffprobe_duration(montage_path))
        caption = (
            f"MLBB shorts | {len(meta.get('selected_segments') or [])} clips | "
            f"{duration:.0f}s | id={meta.get('output_id', montage_path.stem)}"
        )
        ok, detail = verify_and_send(
            montage_path,
            meta,
            chat_id=chat_id,
            token=token,
            caption=caption,
        )
        if not ok:
            return 1, detail
        return 0, f"sent:{montage_path.name}"
    finally:
        Path(queue_path).unlink(missing_ok=True)


def run_cycle(
    *,
    chat_id: str,
    token: str,
    only_paths: list[Path] | None = None,
    max_montages: int | None = None,
) -> dict:
    max_montages = max_montages or MONTAGES_PER_CYCLE
    if only_paths is not None:
        candidates = [p for p in only_paths if p.exists()]
        fresh = filter_new_sources(candidates, chat_id=chat_id)
    else:
        all_paths = gather_candidate_paths()
        fresh = filter_new_sources(all_paths, max_age_hours=SOURCE_MAX_AGE_HOURS)[:MAX_SOURCES_GATHER]

    short_fresh = [p for p in fresh if is_short_source(p)]
    result = {
        "at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "candidates": len(fresh),
        "short_candidates": len(short_fresh),
        "montages_ok": 0,
        "montages_fail": 0,
        "details": [],
    }

    if not short_fresh:
        result["skipped"] = True
        result["reason"] = "no_short_sources"
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        return result

    batches = _chunk(short_fresh, SOURCES_PER_MONTAGE)[:max_montages]
    used_paths: list[Path] = []

    for idx, batch in enumerate(batches, 1):
        code, detail = run_montage_batch(
            batch,
            chat_id=chat_id,
            token=token,
            label=f"MLBB shorts batch {idx}",
        )
        result["details"].append({"batch": idx, "sources": [str(p) for p in batch], "code": code, "detail": detail})
        if code == 0:
            result["montages_ok"] += 1
            used_paths.extend(batch)
        else:
            result["montages_fail"] += 1

    if used_paths:
        mark_used(used_paths, chat_id=chat_id)

    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def main() -> int:
    chat_id = os.environ.get("TG_CHAT_ID", "")
    token = os.environ.get("TG_BOT_TOKEN", "")
    if not chat_id or not token:
        print("MLBB shorts: TG_CHAT_ID/TG_BOT_TOKEN missing")
        return 1

    result = run_cycle(chat_id=chat_id, token=token)
    print(json.dumps(result, ensure_ascii=False))
    if result.get("skipped"):
        return 0
    return 0 if result.get("montages_ok", 0) > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
