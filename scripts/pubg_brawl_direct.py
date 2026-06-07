#!/usr/bin/env python3
"""PUBG Metro: cut montages from verified brawl windows only (no smart scoring guesswork)."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from gameplay_gate import detect_game_viewport_crop
from montage_env import strict_peak_env
from pubg_shooting_gate import format_segment_metrics_line, pubg_passes_shooting_gate
from strict_segment_gate import verify_montage_segments

# Owner-confirmed brawl anchors (n97cHIR9Qow) — not sniper 33:25
FIGHT_ANCHORS_SEC = [1845.0, 2150.0, 2470.0]
CLIP_SEC = 9.5
MIN_CLIPS = 3
TARGET_CLIPS = 4
MIN_FINAL_DURATION = 33.0
MAX_FINAL_DURATION = 57.0
MIN_GAP_SEC = 95.0

GLOBAL_HISTORY = Path("/root/data/mlbb/pubg_global_segment_history.json")
DEFAULT_VOD = Path("/root/data/mlbb/youtube_nightly/inbox/yt_n97cHIR9Qow.mp4")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("pubg_brawl_direct")


@dataclass
class BrawlWindow:
    start: float
    gun: float
    burst: float
    motion: float
    rms: float
    near_anchor: float
    reason: str


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def segment_key(sig: str, start: float) -> str:
    return f"{sig}:{round(start, 3)}"


def resolve_pubg_chat_id(env: dict[str, str]) -> str:
    for key in ("PUBG_CHAT_IDS",):
        raw = env.get(key, "")
        for part in raw.split(","):
            part = part.strip()
            if part:
                return part
    for part in env.get("CHAT_GAME_PROFILES", "").split(","):
        part = part.strip()
        if ":" in part:
            chat_id, profile = part.split(":", 1)
            if profile.strip().lower() == "pubg" and chat_id.strip():
                return chat_id.strip()
    return env.get("TG_CHAT_ID", "")


def apply_pubg_env(env: dict[str, str]) -> dict[str, str]:
    merged = dict(strict_peak_env("pubg"))
    merged.update(env)
    for key, val in merged.items():
        os.environ[key] = val
    return merged


def load_used_keys() -> set[str]:
    used: set[str] = set()
    if GLOBAL_HISTORY.exists():
        try:
            payload = json.loads(GLOBAL_HISTORY.read_text(encoding="utf-8"))
            used |= {str(k) for k in payload.get("segment_keys", [])}
        except (json.JSONDecodeError, OSError):
            pass
    for pattern in ("pubg_*.json", "pubg_tiktok_*.json", "morning_pubg_*.json", "showcase_pubg_*.json"):
        for path in Path("/root/videos").glob(pattern):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            for seg in data.get("selected_segments", []):
                sig = seg.get("source_signature", "")
                if sig:
                    used.add(segment_key(sig, float(seg.get("start", 0))))
    return used


def save_used_keys(keys: set[str]) -> None:
    GLOBAL_HISTORY.parent.mkdir(parents=True, exist_ok=True)
    GLOBAL_HISTORY.write_text(
        json.dumps(
            {
                "segment_keys": sorted(keys),
                "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def nearest_anchor(start: float) -> float:
    return min(abs(start - anchor) for anchor in FIGHT_ANCHORS_SEC)


def probe_window(vod: Path, start: float) -> BrawlWindow | None:
    if start < 0 or start + CLIP_SEC > 20000:
        return None
    ok, reason, metrics = pubg_passes_shooting_gate(vod, start, CLIP_SEC)
    log.info(format_segment_metrics_line(metrics, reason, ok=ok))
    if not ok:
        return None
    return BrawlWindow(
        start=round(start, 3),
        gun=float(metrics["gunfire_density"]),
        burst=float(metrics["burst_ratio"]),
        motion=float(metrics["center_motion"]),
        rms=float(metrics["audio_rms"]),
        near_anchor=nearest_anchor(start),
        reason=reason,
    )


def discover_brawl_pool(vod: Path, sig: str, used: set[str]) -> list[BrawlWindow]:
    pool: list[BrawlWindow] = []
    seen_starts: set[float] = set()

    def add(win: BrawlWindow | None) -> None:
        if win is None:
            return
        key = segment_key(sig, win.start)
        if key in used:
            return
        if any(abs(win.start - s) < 12.0 for s in seen_starts):
            return
        seen_starts.add(win.start)
        pool.append(win)

    for anchor in FIGHT_ANCHORS_SEC:
        for off in (-3.0, -1.5, 0.0, 1.5, 3.0):
            add(probe_window(vod, anchor + off - 4.0))

    try:
        dur_out = subprocess.check_output(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(vod),
            ],
            text=True,
            timeout=30,
        )
        duration = float(dur_out.strip())
    except Exception:
        duration = 10445.0

    for start in range(120, int(duration) - int(CLIP_SEC) - 1, 18):
        add(probe_window(vod, float(start)))

    pool.sort(key=lambda w: (w.near_anchor <= 5.0, w.gun, w.burst, w.motion), reverse=True)
    return pool


def pick_clips(
    pool: list[BrawlWindow],
    used: set[str],
    sig: str,
    *,
    target: int = TARGET_CLIPS,
    minimum: int = MIN_CLIPS,
) -> list[BrawlWindow]:
    chosen: list[BrawlWindow] = []
    for win in pool:
        key = segment_key(sig, win.start)
        if key in used:
            continue
        if any(abs(win.start - c.start) < MIN_GAP_SEC for c in chosen):
            continue
        chosen.append(win)
        if len(chosen) >= target:
            break
    est_duration = len(chosen) * CLIP_SEC
    if len(chosen) < minimum:
        return []
    if est_duration < MIN_FINAL_DURATION and len(chosen) < target:
        return []
    if est_duration > MAX_FINAL_DURATION:
        while len(chosen) > minimum and len(chosen) * CLIP_SEC > MAX_FINAL_DURATION:
            chosen.pop()
    return chosen


def build_montage(
    vod: Path,
    clips: list[BrawlWindow],
    *,
    output_dir: Path,
    basename: str,
    caption: str,
    chat_id: str,
    bot_token: str,
) -> Path | None:
    from smart_video_editor import (
        build_xfade_command,
        ffprobe_duration,
        render_segment,
        run_command,
        send_telegram_video,
        short_file_id,
    )

    sig = file_sha256(vod)
    segment_pairs = [(c.start, CLIP_SEC) for c in clips]
    all_ok, verify_metrics, acceptance_table = verify_montage_segments(vod, "pubg", segment_pairs)
    log.info("\n%s", acceptance_table)
    if not all_ok:
        log.error("abort send: %s segment(s) failed shooting gate", sum(1 for m in verify_metrics if not m.get("pass")))
        return None

    logo = Path(os.environ.get("LOGO_FILE", "/root/logo.png"))
    temp_dir = Path(tempfile.mkdtemp(prefix="pubg-brawl-"))
    try:
        candidates: list[dict] = []
        for win in clips:
            crop = detect_game_viewport_crop(vod, win.start, CLIP_SEC)
            candidates.append(
                {
                    "source_index": 0,
                    "source_signature": sig,
                    "source_path": str(vod),
                    "game_name": "PUBG Metro",
                    "start": win.start,
                    "output_duration": CLIP_SEC,
                    "input_duration": CLIP_SEC,
                    "speed": 1.0,
                    "score": round(win.gun * 10 + win.motion, 4),
                    "crop_box": list(crop) if crop else None,
                }
            )

        segment_paths: list[Path] = []
        durations: list[float] = []
        for idx, cand in enumerate(candidates):
            seg_path = temp_dir / f"seg_{idx:02d}.mp4"
            durations.append(render_segment(cand, seg_path, logo))
            segment_paths.append(seg_path)
            cand["rendered_duration"] = round(durations[-1], 3)

        out = output_dir / f"{basename}_{time.strftime('%Y%m%d_%H%M%S')}.mp4"
        run_command(build_xfade_command(segment_paths, durations, out))
        final_dur = ffprobe_duration(out)

        if final_dur < MIN_FINAL_DURATION or final_dur > MAX_FINAL_DURATION:
            log.error("abort send: final duration %.1fs outside %.0f-%.0fs", final_dur, MIN_FINAL_DURATION, MAX_FINAL_DURATION)
            return None

        meta = {
            "profile": "pubg",
            "mode": "strict_peak",
            "final_duration": final_dur,
            "output_id": short_file_id(out),
            "sources": [{"path": str(vod), "game_name": "PUBG Metro"}],
            "selected_segments": candidates,
            "acceptance_table": acceptance_table,
            "segment_metrics": verify_metrics,
            "brawl_metrics": verify_metrics,
        }
        out.with_suffix(".json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

        if bot_token and chat_id:
            send_telegram_video(bot_token, chat_id, out, caption)
        return out
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def make_brawl_montage(
    *,
    output_basename: str,
    caption: str,
    env: dict[str, str],
    vod: Path = DEFAULT_VOD,
) -> tuple[int, str]:
    if not vod.exists():
        return 2, f"vod missing: {vod}"

    from strict_montage_direct import build_and_send, discover_strict_candidates, pick_segments

    merged_env = apply_pubg_env(env)
    sig = file_sha256(vod)
    used = load_used_keys()
    pool = discover_strict_candidates(vod, "pubg", sig, used)
    if len(pool) < MIN_CLIPS:
        return 1, f"Game=PUBG, found {len(pool)}/{MIN_CLIPS} strict segments"

    clips = pick_segments(pool, used, sig)
    if len(clips) < MIN_CLIPS:
        return 1, f"Game=PUBG, found {len(clips)}/{MIN_CLIPS} non-overlapping strict segments"

    out_dir = Path(merged_env.get("OUTPUT_DIR", "/root/videos"))
    out_dir.mkdir(parents=True, exist_ok=True)
    chat_id = resolve_pubg_chat_id(merged_env)
    result = build_and_send(
        vod,
        "pubg",
        clips,
        output_dir=out_dir,
        basename=output_basename,
        caption=caption,
        chat_id=chat_id,
        bot_token=merged_env.get("TG_BOT_TOKEN", ""),
        sig=sig,
    )
    if result is None:
        return 1, "REFUSED: game=PUBG, reason=render_or_visual_gate_failed, visual_passed=0/0"

    for clip in clips:
        used.add(segment_key(sig, float(clip["start"])))
    save_used_keys(used)
    return 3, f"REFUSED: game=PUBG, reason=awaiting_owner_preview, visual_passed={len(clips)}/{len(clips)}, montage={result.name}"
