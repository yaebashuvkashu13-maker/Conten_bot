#!/usr/bin/env python3
"""Re-render shooter VOD segments with timing offset and send preview."""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from daily_game_cycle import profile_for_game
from mlbb_vod_segment_feed import (
    _ffprobe_duration,
    render_single_segment,
    send_message,
    send_video,
)
from shooter_vod_segment_store import (
    keyboard,
    mark_feed_sent,
    segment_id,
    upsert_segment,
    vod_youtube_id,
    _paths,
)
from shooter_vod_timing import peak_lag_sec, window_times
from strict_montage_direct import file_sha256
from youtube_download import load_env


def parse_segment_id(raw: str) -> tuple[str, float]:
    sid = raw.strip().lstrip("#")
    if "_" not in sid:
        raise ValueError(f"bad segment id: {raw}")
    vid, start_s = sid.rsplit("_", 1)
    return vid, float(start_s)


def redo_segment(
    game: str,
    segment_id_raw: str,
    *,
    token: str,
    chat_id: str,
    peak_lag_override: float | None = None,
) -> int:
    game = game.strip().lower()
    vid, old_start = parse_segment_id(segment_id_raw)
    sid_old = segment_id_raw.strip().lstrip("#")

    inbox = _paths(game)["inbox"]
    vod = inbox / f"yt_{vid}.mp4"
    if not vod.exists():
        for p in inbox.glob(f"yt_*{vid}*.mp4"):
            vod = p
            break
    if not vod.exists():
        send_message(token, chat_id, f"❌ Нет VOD {vid} в inbox")
        return 0

    lead = float(os.environ.get("MLBB_VOD_LEAD_SEC", "4"))
    if peak_lag_override is not None:
        os.environ["SHOOTER_VOD_PEAK_LAG_SEC"] = str(peak_lag_override)
    lag = peak_lag_sec(game)

    from shooter_vod_segment_store import find_segment

    row = find_segment(game, sid_old) or {}
    if row.get("peak_start") is not None:
        peak_raw = float(row["peak_start"])
        # Peaks stored after timing fix already include lag — avoid double shift.
        if row.get("timing_redo_from") or lag <= 0:
            pass
        elif peak_raw > old_start + lead + 0.5:
            peak_raw = max(lead, peak_raw - lag)
    else:
        peak_raw = old_start + lead

    start, peak_eff, dur = window_times(game, peak_raw)

    new_sid = segment_id(vid, start)
    out = _paths(game)["segments"] / f"seg_{new_sid}.mp4"
    clip = {
        "start": start,
        "peak_start": peak_eff,
        "input_duration": dur,
        "output_duration": dur,
        "score": 1.0,
    }

    if not render_single_segment(vod, clip, out):
        send_message(token, chat_id, f"❌ render fail {new_sid}")
        return 0

    caption = (
        f"{game.upper()} Metro Royale #{new_sid}\n"
        f"{vid} @ {int(start)}s (пик {int(peak_eff)}s, +{int(lag)}s)\n"
        f"Metro ✓ | timing_fix\n"
        f"👍 Ок / 👎 Не ок"
    )
    if not send_video(
        token,
        chat_id,
        out,
        caption,
        seg_id=new_sid,
        record_learning=False,
        reply_markup=keyboard(game, new_sid),
        cycle_game=game,
    ):
        send_message(token, chat_id, f"⚠️ TG send fail {new_sid}")
        return 0

    sig = file_sha256(vod)
    upsert_segment(
        game,
        {
            "segment_id": new_sid,
            "path": str(out),
            "vod": str(vod),
            "vod_id": vod_youtube_id(vod),
            "start": start,
            "duration": _ffprobe_duration(out),
            "peak_start": peak_eff,
            "score": 1.0,
            "sig": sig,
            "ingested_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "timing_redo_from": sid_old,
        },
    )
    mark_feed_sent(game, [new_sid])

    from vod_owner_learning import append_owner_time_label

    profile = profile_for_game(game)
    append_owner_time_label(
        profile,
        vid,
        peak_eff,
        "good",
        note=f"timing_lag_{int(lag)}s redo_from_{sid_old}",
        source="timing_fix",
    )
    send_message(token, chat_id, f"✅ Перерезка {sid_old} → {new_sid} (+{int(lag)}s)")
    return 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("game", choices=["pubg", "standoff", "genshin", "wot"])
    parser.add_argument("segment_id")
    parser.add_argument("--lag-sec", type=float, default=None)
    args = parser.parse_args()

    env = load_env(Path("/root/.video_bot.env"))
    token = env.get("TELEGRAM_BOT_TOKEN") or env.get("TG_BOT_TOKEN", "")
    chat_id = env.get("TELEGRAM_CHAT_ID") or env.get("TG_CHAT_ID", "")
    if not token or not chat_id:
        print("telegram env missing", file=sys.stderr)
        return 1

    os.environ.setdefault("SHOOTER_VOD_SEND_AS_VIDEO", "1")
    n = redo_segment(
        args.game,
        args.segment_id,
        token=token,
        chat_id=chat_id,
        peak_lag_override=args.lag_sec,
    )
    return 0 if n else 1


if __name__ == "__main__":
    raise SystemExit(main())
