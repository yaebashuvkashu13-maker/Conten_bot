#!/usr/bin/env python3
"""Render + send one PUBG fight peak as HQ file (source quality, not Telegram montage)."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


def _resolve_vod(vid: str) -> Path | None:
    name = vid if vid.endswith(".mp4") else f"yt_{vid}.mp4"
    for base in (
        Path("/root/data/pubg/youtube_nightly/inbox"),
        Path("/root/data/mlbb/youtube_nightly/inbox"),
    ):
        candidate = base / name
        if candidate.is_file():
            return candidate
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description="Send HQ PUBG peak clip")
    parser.add_argument("--vod", required=True)
    parser.add_argument("--peak", type=float, required=True)
    parser.add_argument("--chat-id", default="")
    parser.add_argument("--crf", default="15")
    parser.add_argument("--preset", default="slow")
    args = parser.parse_args()

    from vod_env import load_env

    for key, val in load_env().items():
        os.environ.setdefault(key, val)
    os.environ["PUBG_FIGHT_SEGMENTER"] = "1"
    os.environ["MLBB_VOD_ENCODE_CRF"] = str(args.crf)
    os.environ["MLBB_VOD_ENCODE_PRESET"] = str(args.preset)
    os.environ["SMART_OUTPUT_PRESET"] = str(args.preset)

    vod = _resolve_vod(args.vod)
    if vod is None:
        print(f"REFUSED vod_missing {args.vod}")
        return 2

    from pubg_fight_segment import resolve_pubg_fight_bounds
    from pubg_montage_bounds import tighten_pubg_clip_bounds
    from shooter_vod_segment_feed import _ffprobe_duration, _prepare_montage_clip, _montage_limits
    from shooter_vod_segment_store import peak_label_sec, upsert_segment, _paths
    from mlbb_vod_segment_feed import render_single_segment
    from mlbb_telegram_video import send_hq_files

    peak = float(args.peak)
    file_dur = _ffprobe_duration(vod)
    start, dur, report = resolve_pubg_fight_bounds(vod, peak, file_duration=file_dur)
    start, dur = tighten_pubg_clip_bounds(start, dur, report)
    _min, _max, _gap, part_max, _final = _montage_limits()
    row = {
        "segment_id": f"owner_{vod.stem}_{peak_label_sec(peak)}",
        "peak_start": peak,
        "start": start,
        "clip": {"start": start, "peak_start": peak},
    }
    clip = _prepare_montage_clip(row, vod, part_max=part_max, game="pubg")
    seg_root = _paths("pubg")["segments"]
    seg_root.mkdir(parents=True, exist_ok=True)
    out = seg_root / f"seg_{row['segment_id']}.mp4"
    if not render_single_segment(vod, clip, out):
        print(f"REFUSED render_failed peak={peak}")
        return 1

    upsert_segment(
        "pubg",
        {
            "segment_id": row["segment_id"],
            "path": str(out),
            "vod": str(vod),
            "vod_id": vod.stem[3:] if vod.stem.startswith("yt_") else vod.stem,
            "start": clip["start"],
            "duration": clip.get("output_duration", clip.get("input_duration", dur)),
            "peak_start": peak,
            "segment_report": report,
            "hq_render": True,
        },
    )

    token = os.environ.get("TG_BOT_TOKEN", "")
    chat_id = (
        args.chat_id.strip()
        or os.environ.get("TG_CHAT_ID", "").strip()
        or os.environ.get("PUBG_OWNER_REDO_CHAT_ID", "").strip()
    )
    if not token or not chat_id:
        print(f"OK rendered {out} (no telegram)")
        return 0

    caption = (
        f"PUBG HQ #{row['segment_id']}\n"
        f"VOD {vod.stem[3:] if vod.stem.startswith('yt_') else vod.stem}\n"
        f"peak={peak_label_sec(peak)}s (raw {peak:.1f}s)\n"
        f"CRF {args.crf} · без пережатия Telegram"
    )
    ok = send_hq_files(
        token,
        chat_id,
        out,
        caption,
        filename=f"PUBG_{row['segment_id']}.mp4",
    )
    print("OK sent" if ok else "REFUSED send_failed", out)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
