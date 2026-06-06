#!/usr/bin/env python3
"""PUBG Metro: cut montages from verified brawl windows only (no smart scoring guesswork)."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from gameplay_gate import (
    detect_game_viewport_crop,
    score_pubg_gunfire_audio,
    score_segment_combat,
)

# Owner-confirmed brawl anchors (n97cHIR9Qow) — not sniper 33:25
FIGHT_ANCHORS_SEC = [1845.0, 2150.0, 2470.0]
CLIP_SEC = 9.5
CLIPS_PER_MONTAGE = 5
MIN_GAP_SEC = 95.0

GLOBAL_HISTORY = Path("/root/data/mlbb/pubg_global_segment_history.json")
DEFAULT_VOD = Path("/root/data/mlbb/youtube_nightly/inbox/yt_n97cHIR9Qow.mp4")


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


def qualifies_brawl(start: float, gun: float, burst: float, motion: float, rms: float) -> tuple[bool, str]:
    dist = nearest_anchor(start)
    if rms > 0.050 and gun < 0.020:
        return False, "talk"
    if motion > 0.24 and gun < 0.060:
        return False, "run"
    if dist <= 5.0:
        if gun >= 0.052 and burst >= 4.6 and motion >= 0.035:
            return True, f"anchor_dist{dist:.0f}"
        return False, f"weak_anchor_gun{gun:.3f}"
    if gun >= 0.080 and burst >= 5.8 and motion >= 0.050:
        return True, "hot_scan"
    return False, f"low_gun{gun:.3f}"


def probe_window(vod: Path, start: float) -> BrawlWindow | None:
    if start < 0 or start + CLIP_SEC > 20000:
        return None
    crop = detect_game_viewport_crop(vod, start, CLIP_SEC)
    gun, burst, rms = score_pubg_gunfire_audio(vod, start, CLIP_SEC)
    motion, _, _, _ = score_segment_combat(vod, start, CLIP_SEC, crop_box=crop, sample_frames=5)
    ok, reason = qualifies_brawl(start, gun, burst, motion, rms)
    if not ok:
        return None
    return BrawlWindow(
        start=round(start, 3),
        gun=gun,
        burst=burst,
        motion=motion,
        rms=rms,
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


def pick_clips(pool: list[BrawlWindow], used: set[str], sig: str, count: int = CLIPS_PER_MONTAGE) -> list[BrawlWindow]:
    chosen: list[BrawlWindow] = []
    for win in pool:
        key = segment_key(sig, win.start)
        if key in used:
            continue
        if any(abs(win.start - c.start) < MIN_GAP_SEC for c in chosen):
            continue
        chosen.append(win)
        if len(chosen) >= count:
            break
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

        meta = {
            "profile": "pubg",
            "mode": "brawl_direct",
            "final_duration": final_dur,
            "output_id": short_file_id(out),
            "sources": [{"path": str(vod), "game_name": "PUBG Metro"}],
            "selected_segments": candidates,
            "brawl_metrics": [
                {
                    "start": c["start"],
                    "gun": clips[i].gun,
                    "burst": clips[i].burst,
                    "motion": clips[i].motion,
                    "reason": clips[i].reason,
                }
                for i, c in enumerate(candidates)
            ],
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

    sig = file_sha256(vod)
    used = load_used_keys()
    pool = discover_brawl_pool(vod, sig, used)
    if len(pool) < CLIPS_PER_MONTAGE:
        return 1, f"only {len(pool)} brawl windows (need {CLIPS_PER_MONTAGE})"

    clips = pick_clips(pool, used, sig, CLIPS_PER_MONTAGE)
    if len(clips) < CLIPS_PER_MONTAGE:
        return 1, f"only {len(clips)} non-overlapping clips"

    out_dir = Path(env.get("OUTPUT_DIR", "/root/videos"))
    out_dir.mkdir(parents=True, exist_ok=True)
    for key, val in env.items():
        os.environ.setdefault(key, val)

    result = build_montage(
        vod,
        clips,
        output_dir=out_dir,
        basename=output_basename,
        caption=caption,
        chat_id=env.get("TG_CHAT_ID", ""),
        bot_token=env.get("TG_BOT_TOKEN", ""),
    )
    if result is None:
        return 1, "render failed"

    for clip in clips:
        used.add(segment_key(sig, clip.start))
    save_used_keys(used)
    return 0, result.name
