#!/usr/bin/env python3
"""Owner timestamp cuts: download VOD, trim fights near anchors, send preview clips."""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mlbb_fight_segment import clear_analysis_cache, detect_fight_bounds
from mlbb_vod_oneoff import (
    ENV_PATH,
    INBOX,
    _video_id,
    download_vod_exact,
    pause_worker,
    resume_worker,
)
from mlbb_vod_segment_feed import (
    _normalize_clip,
    render_single_segment,
    send_message,
    send_video,
)
from mlbb_vod_segment_store import segments_root
from nightly_youtube_montage import fetch_video_meta
from youtube_download import load_env

PREVIEW_ROOT = Path(os.environ.get("MLBB_OWNER_PREVIEW_ROOT", "/root/datasets/mlbb/owner_previews"))


def parse_time(text: str) -> float:
    """Parse 360, 6, 6:00, 7:30, 8:20 into seconds."""
    raw = text.strip().lower().rstrip("s")
    if re.fullmatch(r"\d+(\.\d+)?", raw):
        val = float(raw)
        return val if val > 120 else val * 60.0
    if ":" in raw:
        parts = raw.split(":")
        if len(parts) == 2:
            return int(parts[0]) * 60 + float(parts[1])
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
    raise ValueError(f"bad time: {text}")


def refine_peak_near(vod: Path, anchor_sec: float, *, radius: float = 50.0) -> float:
    """Snap owner anchor to local action peak using one cached VOD analysis."""
    from mlbb_fight_segment import _analysis_for

    analysis = _analysis_for(vod)
    win = float(analysis.get("window_seconds", 2.0))
    motion_raw = analysis.get("center_motion")
    audio_raw = analysis.get("audio")
    motion = np.asarray(motion_raw if motion_raw is not None else [], dtype=np.float32)
    audio = np.asarray(audio_raw if audio_raw is not None else [], dtype=np.float32)
    if motion.size < 2:
        return anchor_sec
    combined = motion * 0.48 + (audio if audio.size == motion.size else motion) * 0.52
    i0 = max(0, int((anchor_sec - radius) / win))
    i1 = min(len(combined), int((anchor_sec + radius) / win) + 1)
    if i1 <= i0:
        return anchor_sec
    local = combined[i0:i1]
    peak_i = int(np.argmax(local)) + i0
    refined = peak_i * win + win * 0.5
    if abs(refined - anchor_sec) > radius:
        return anchor_sec
    return round(refined, 1)


def _fmt_ts(sec: float) -> str:
    sec = max(0.0, sec)
    m, s = divmod(int(sec), 60)
    return f"{m}:{s:02d}"


def cut_and_send(
    url: str,
    anchors: list[float],
    *,
    env: dict[str, str],
    token: str,
    chat_id: str,
    preview: bool = True,
) -> int:
    vid = _video_id(url)
    if len(vid) != 11:
        raise ValueError(f"bad url: {url}")

    os.environ.setdefault("MLBB_VOD_VARIABLE_LENGTH", "1")
    os.environ.setdefault("MLBB_VOD_NO_CROP", "1")
    os.environ.setdefault("MLBB_FIGHT_MAX_SEC", "42")
    os.environ.setdefault("MLBB_FIGHT_SUSTAIN_QUIET_BINS", "4")
    os.environ.setdefault("MLBB_VOD_LEAD_SEC", "4")
    os.environ.setdefault("MLBB_FORCE_RERENDER", "1")
    os.environ.setdefault("SMART_OUTPUT_PRESET", "fast")
    os.environ.setdefault("SMART_OUTPUT_CRF", "23")
    os.environ.setdefault("SMART_OUTPUT_AUDIO_K", "128")

    INBOX.mkdir(parents=True, exist_ok=True)
    dest = INBOX / f"yt_{vid}.mp4"
    if not dest.exists() or dest.stat().st_size < 500_000:
        send_message(token, chat_id, f"📥 Качаю VOD {vid} для нарезок…")
        dest = download_vod_exact(url, dest, env)

    meta = fetch_video_meta(vid, env) or {}
    title = str(meta.get("title") or vid)[:100]
    send_message(
        token,
        chat_id,
        f"✂️ {title}\nРежу {len(anchors)} примера по твоим меткам…",
    )

    clear_analysis_cache()
    PREVIEW_ROOT.mkdir(parents=True, exist_ok=True)
    sent = 0
    for idx, anchor in enumerate(anchors, 1):
        peak = refine_peak_near(dest, anchor)
        clip = {"start": peak, "score": 1.0, "hook_score": 0.0}
        norm = _normalize_clip(clip, dest)
        start = float(norm["start"])
        dur = float(norm["input_duration"])
        end = start + dur
        sid = f"{vid}_{int(peak)}"
        out = PREVIEW_ROOT / f"preview_{sid}.mp4"
        if not render_single_segment(dest, norm, out):
            send_message(token, chat_id, f"❌ Не вышло нарезать #{idx} @ {_fmt_ts(anchor)}")
            continue
        caption = (
            f"MLBB пример v2 #{idx}/{len(anchors)}\n"
            f"{vid} | метка {_fmt_ts(anchor)} → пик {_fmt_ts(peak)}\n"
            f"окно {_fmt_ts(start)}–{_fmt_ts(end)} ({dur:.0f}с)\n"
            f"полный кадр · extended fight\n"
            f"👍 Ок / 👎 Не ок"
        )
        if send_video(token, chat_id, out, caption, seg_id=sid):
            sent += 1
            time.sleep(1.5)
        else:
            send_message(token, chat_id, f"⚠️ #{idx} не ушло в TG (>20MB?) — {out.name}")

    send_message(token, chat_id, f"Готово {vid}: {sent}/{len(anchors)} примеров")
    print(f"owner_cuts_done vid={vid} sent={sent}/{len(anchors)}")
    return sent


def main() -> int:
    import logging

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    parser = argparse.ArgumentParser(description="Cut owner timestamps and send preview clips")
    parser.add_argument("url")
    parser.add_argument(
        "--at",
        dest="anchors",
        nargs="+",
        required=True,
        help="Anchor times: 6:00 7:30 8:20 or 360 450",
    )
    parser.add_argument("--no-resume-worker", action="store_true")
    args = parser.parse_args()

    try:
        anchors = [parse_time(a) for a in args.anchors]
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 1

    os.environ.setdefault("MLBB_ONLY_MODE", "1")
    os.environ.setdefault("MLBB_SEND_ENABLED", "1")
    os.environ.setdefault("MLBB_LEARNING_FIRST", "0")

    env = {**os.environ, **load_env(ENV_PATH)}
    token = env.get("TG_BOT_TOKEN", "")
    chat_id = env.get("TG_CHAT_ID", "")
    if not token or not chat_id:
        print("TG missing", file=sys.stderr)
        return 1

    pause_worker()
    try:
        sent = cut_and_send(args.url, anchors, env=env, token=token, chat_id=chat_id, preview=True)
        return 0 if sent > 0 else 1
    except Exception as exc:
        send_message(token, chat_id, f"❌ Owner cuts: {exc}")
        raise
    finally:
        if not args.no_resume_worker:
            resume_worker()


if __name__ == "__main__":
    raise SystemExit(main())
