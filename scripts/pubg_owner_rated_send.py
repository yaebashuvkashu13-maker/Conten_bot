#!/usr/bin/env python3
"""Send a PUBG owner clip with rating buttons (👍/👎) always attached.

Ad-hoc force scripts that call Telegram sendVideo without reply_markup drop the
owner keyboard (Wg9qrAzWTLU @ 12:00). Use this helper for every manual/owner send.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from mlbb_telegram_video import send_video_file
from shooter_vod_segment_store import (
    keyboard,
    mark_feed_sent,
    segment_id as make_sid,
    upsert_segment,
)
from telegram_delivery import encode_telegram_mp4 as encode_for_telegram


def _load_env() -> None:
    env_path = Path(os.environ.get("VIDEO_BOT_ENV", "/root/.video_bot.env"))
    if not env_path.is_file():
        return
    for line in env_path.read_text(errors="ignore").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


def _ffprobe_duration(path: Path) -> float:
    try:
        out = subprocess.check_output(
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
            text=True,
            timeout=30,
        )
        return float(out.strip() or 0.0)
    except Exception:
        return 0.0


def send_owner_rated_clip(
    video_path: Path,
    *,
    caption: str,
    vod: Path | None = None,
    start_sec: float = 0.0,
    peak_sec: float | None = None,
    chat_id: str | None = None,
    token: str | None = None,
    segment_id: str | None = None,
) -> dict:
    """Encode if needed, register segment, sendVideo with inline 👍/👎 keyboard."""
    _load_env()

    token = (token or os.environ.get("TG_BOT_TOKEN") or "").strip()
    chat_id = (
        chat_id
        or os.environ.get("TG_CHAT_ID")
        or os.environ.get("PUBG_OWNER_CHAT_ID")
        or ""
    ).strip()
    if not token or not chat_id:
        raise RuntimeError("TG_BOT_TOKEN / TG_CHAT_ID missing")

    video_path = Path(video_path)
    if not video_path.is_file():
        raise FileNotFoundError(video_path)

    vod = Path(vod) if vod else video_path
    peak = float(peak_sec if peak_sec is not None else start_sec)
    vod_key = vod.stem
    if vod_key.startswith("yt_"):
        vod_key = vod_key[3:]
    sid = (segment_id or make_sid(vod_key, float(start_sec))).strip()

    deliver = encode_for_telegram(video_path)
    markup = keyboard("pubg", sid)
    rows = (markup or {}).get("inline_keyboard") or []
    if not rows:
        raise RuntimeError("rating keyboard empty — refuse send without buttons")

    ok = send_video_file(
        token,
        chat_id,
        Path(deliver),
        caption,
        reply_markup=markup,
    )
    if not ok:
        raise RuntimeError("telegram send_video_file failed")

    upsert_segment(
        "pubg",
        {
            "segment_id": sid,
            "path": str(deliver),
            "vod": str(vod),
            "vod_id": vod_key,
            "start": float(start_sec),
            "duration": _ffprobe_duration(Path(deliver)),
            "peak_start": peak,
            "ingested_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "owner_rated_send": True,
        },
    )
    mark_feed_sent("pubg", [sid])
    return {"ok": True, "segment_id": sid, "path": str(deliver), "reply_markup": markup}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--video", required=True, type=Path)
    parser.add_argument("--caption", required=True)
    parser.add_argument("--vod", type=Path, default=None)
    parser.add_argument("--start", type=float, default=0.0)
    parser.add_argument("--peak", type=float, default=None)
    parser.add_argument("--chat-id", default="")
    args = parser.parse_args()
    result = send_owner_rated_clip(
        args.video,
        caption=args.caption,
        vod=args.vod,
        start_sec=args.start,
        peak_sec=args.peak,
        chat_id=args.chat_id or None,
    )
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
