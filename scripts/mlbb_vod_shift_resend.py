#!/usr/bin/env python3
"""Re-render and resend an MLBB VOD segment shifted earlier/later on the timeline."""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mlbb_vod_oneoff import ENV_PATH, pause_worker, resume_worker
from mlbb_vod_segment_feed import (
    _format_send_report,
    _segment_duration_from_row,
    _validate_before_send,
    render_single_segment,
    send_message,
    send_video,
)
from mlbb_vod_segment_store import find_segment, segment_id, segments_root, upsert_segment, vod_youtube_id
from youtube_download import load_env


def shift_resend(
    segment_id_str: str,
    *,
    shift_sec: float,
    token: str,
    chat_id: str,
    skip_presend: bool = False,
) -> bool:
    sid = segment_id_str.strip().lstrip("#")
    row = find_segment(sid)
    if not row:
        send_message(token, chat_id, f"❌ Не найден сегмент {sid}")
        return False

    vod = Path(row.get("vod") or "")
    if not vod.exists():
        vid = str(row.get("vod_id") or sid.rsplit("_", 1)[0])
        vod = Path(f"/root/data/mlbb/youtube_nightly/inbox/yt_{vid}.mp4")
    if not vod.exists():
        send_message(token, chat_id, f"❌ Нет VOD для {sid}")
        return False

    old_start = float(row.get("start", 0))
    dur = float(row.get("duration") or row.get("fight_dur") or 28.0)
    peak = float(row.get("peak_start") or old_start)
    new_start = max(0.0, round(old_start + shift_sec, 2))
    new_sid = segment_id(vod, new_start)

    clip = {
        "start": new_start,
        "peak_start": peak,
        "input_duration": dur,
        "output_duration": dur,
        "score": float(row.get("score") or 0),
        "hook_score": float(row.get("hook_score") or 0),
        "clip_score": row.get("clip_score"),
        "pass_reason": row.get("pass_reason") or "mlbb_fight_ok",
    }

    out = segments_root() / f"seg_{new_sid}.mp4"
    if not render_single_segment(vod, clip, out):
        send_message(token, chat_id, f"❌ render fail {new_sid}")
        return False

    send_row = {
        "segment_id": new_sid,
        "start": new_start,
        "peak_start": peak,
        "clip": clip,
        "score": clip["score"],
        "hook_score": clip["hook_score"],
        "clip_score": clip.get("clip_score"),
        "pass_reason": clip["pass_reason"],
    }

    presend_report: dict = {}
    if not skip_presend:
        ok, reason, presend_report = _validate_before_send(vod, send_row, out)
        if not ok:
            send_message(token, chat_id, f"❌ presend {new_sid}: {reason}")
            return False
    else:
        presend_report = {"pass_reason": "shift_resend_skip_presend"}

    seg_dur = _segment_duration_from_row(send_row) or dur
    report_line = _format_send_report(send_row, presend_report)
    clip_line = ""
    if clip.get("clip_score") is not None:
        clip_line = f"learn={float(clip['clip_score']):.3f} | "
    caption = (
        f"MLBB кусок #{new_sid}\n"
        f"↩️ сдвиг {shift_sec:+.0f}с от #{sid}\n"
        f"{vod_youtube_id(vod)} @ {int(new_start)}s | {seg_dur:.0f}с\n"
        f"{clip_line}{report_line}\n"
        f"cut@{int(new_start)}s peak@{int(peak)}s\n"
        f"✓ presend\n"
        f"👍 Ок / 👎 Не ок"
    )

    if not send_video(token, chat_id, out, caption, seg_id=new_sid):
        send_message(token, chat_id, f"⚠️ TG send fail {new_sid}")
        return False

    upsert_segment(
        {
            "segment_id": new_sid,
            "path": str(out),
            "vod": str(vod),
            "vod_id": vod_youtube_id(vod),
            "start": new_start,
            "duration": seg_dur,
            "fight_dur": dur,
            "peak_start": peak,
            "score": clip["score"],
            "hook_score": clip["hook_score"],
            "clip_score": clip.get("clip_score"),
            "shifted_from": sid,
            "shift_sec": shift_sec,
            "ingested_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
    )
    send_message(token, chat_id, f"✅ Отправлено #{new_sid} (было #{sid}, −{abs(shift_sec):.0f}с)")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="Shift MLBB segment cut and resend to Telegram")
    parser.add_argument("segment_id", help="e.g. GoOXkCDa-9k_585")
    parser.add_argument(
        "--earlier",
        type=float,
        default=None,
        help="Shift cut N seconds earlier (e.g. 7)",
    )
    parser.add_argument(
        "--shift-sec",
        type=float,
        default=None,
        help="Signed shift in seconds (negative = earlier)",
    )
    parser.add_argument("--skip-presend", action="store_true")
    parser.add_argument("--no-resume-worker", action="store_true")
    args = parser.parse_args()

    if args.earlier is not None:
        shift = -abs(args.earlier)
    elif args.shift_sec is not None:
        shift = float(args.shift_sec)
    else:
        parser.error("use --earlier 7 or --shift-sec -7")

    env = {**os.environ, **load_env(ENV_PATH)}
    token = env.get("TG_BOT_TOKEN", "").strip()
    chat_id = env.get("TG_CHAT_ID", "").strip()
    if not token or not chat_id:
        print("TG_BOT_TOKEN / TG_CHAT_ID missing", file=sys.stderr)
        return 1

    os.environ.setdefault("MLBB_VOD_NO_CROP", "1")
    os.environ.setdefault("MLBB_FORCE_RERENDER", "1")
    os.environ.setdefault("VOD_CALIBRATION_SEND_AS_FILE", "0")

    pause_worker()
    try:
        ok = shift_resend(
            args.segment_id,
            shift_sec=shift,
            token=token,
            chat_id=chat_id,
            skip_presend=args.skip_presend,
        )
        return 0 if ok else 1
    finally:
        if not args.no_resume_worker:
            resume_worker()


if __name__ == "__main__":
    raise SystemExit(main())
