#!/usr/bin/env python3
"""Re-render picked segments: full frame, extended fight bounds, send preview."""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mlbb_fight_segment import clear_analysis_cache
from mlbb_vod_oneoff import ENV_PATH, INBOX, pause_worker, resume_worker
from mlbb_vod_segment_feed import (
    _ffprobe_duration,
    _normalize_clip,
    render_single_segment,
    send_message,
    send_video,
)
from mlbb_vod_segment_store import segments_root, upsert_segment
from youtube_download import load_env


def parse_segment_id(raw: str) -> tuple[str, float]:
    sid = raw.strip().lstrip("#")
    if "_" not in sid:
        raise ValueError(f"bad segment id: {raw}")
    vid, peak_s = sid.rsplit("_", 1)
    return vid, float(peak_s)


def redo_segments(segment_ids: list[str], *, token: str, chat_id: str) -> int:
    os.environ.setdefault("MLBB_ONLY_MODE", "1")
    os.environ.setdefault("MLBB_SEND_ENABLED", "1")
    os.environ.setdefault("MLBB_LEARNING_FIRST", "0")
    os.environ.setdefault("MLBB_VOD_VARIABLE_LENGTH", "1")
    os.environ.setdefault("MLBB_VOD_NO_CROP", "1")
    os.environ.setdefault("MLBB_FIGHT_MAX_SEC", "42")
    os.environ.setdefault("MLBB_FIGHT_SUSTAIN_QUIET_BINS", "4")
    os.environ.setdefault("MLBB_VOD_LEAD_SEC", "4")
    os.environ.setdefault("MLBB_FORCE_RERENDER", "1")
    os.environ.setdefault("SMART_OUTPUT_PRESET", "fast")

    clear_analysis_cache()
    segments_root().mkdir(parents=True, exist_ok=True)
    sent = 0

    send_message(
        token,
        chat_id,
        f"🔁 Перерезка {len(segment_ids)} кусков\n"
        f"полный кадр + длиннее до конца тимфайта",
    )

    for raw in segment_ids:
        sid = raw.strip().lstrip("#")
        vid, peak = parse_segment_id(sid)
        vod = INBOX / f"yt_{vid}.mp4"
        if not vod.exists():
            send_message(token, chat_id, f"❌ Нет VOD {vid} на диске")
            continue

        clip = {"start": peak, "score": 1.0, "hook_score": 0.0}
        norm = _normalize_clip(clip, vod)
        start = float(norm["start"])
        dur = float(norm["input_duration"])
        end = start + dur
        out = segments_root() / f"seg_{sid}.mp4"

        if not render_single_segment(vod, norm, out):
            send_message(token, chat_id, f"❌ render fail {sid}")
            continue

        m, s = divmod(int(start), 60)
        em, es = divmod(int(end), 60)
        pm, ps = divmod(int(peak), 60)
        caption = (
            f"MLBB v2 #{sid}\n"
            f"пик {pm}:{ps:02d} | окно {m}:{s:02d}–{em}:{es:02d} ({dur:.0f}с)\n"
            f"full frame · extended fight\n"
            f"👍 Ок / 👎 Не ок"
        )
        if send_video(token, chat_id, out, caption, seg_id=sid):
            upsert_segment(
                {
                    "segment_id": sid,
                    "path": str(out),
                    "vod": str(vod),
                    "vod_id": vid,
                    "start": start,
                    "score": 1.0,
                    "hook_score": 0.0,
                    "sig": "",
                    "ingested_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                }
            )
            sent += 1
            time.sleep(1.5)
        else:
            send_message(token, chat_id, f"⚠️ TG send fail {sid} ({out.stat().st_size // 1_000_000}MB)")

    send_message(token, chat_id, f"Готово v2: {sent}/{len(segment_ids)} кусков")
    print(f"redo_done sent={sent}/{len(segment_ids)}")
    return sent


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("segments", nargs="+", help="e.g. ZEc14HrLBq8_588 hoV3DqtHS0Q_437")
    parser.add_argument("--no-resume-worker", action="store_true")
    args = parser.parse_args()

    env = {**os.environ, **load_env(ENV_PATH)}
    token = env.get("TG_BOT_TOKEN", "")
    chat_id = env.get("TG_CHAT_ID", "")
    if not token or not chat_id:
        print("TG missing", file=sys.stderr)
        return 1

    pause_worker()
    try:
        n = redo_segments(args.segments, token=token, chat_id=chat_id)
        return 0 if n > 0 else 1
    finally:
        if not args.no_resume_worker:
            resume_worker()


if __name__ == "__main__":
    raise SystemExit(main())
