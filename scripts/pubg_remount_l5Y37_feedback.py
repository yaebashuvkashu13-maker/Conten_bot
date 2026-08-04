#!/usr/bin/env python3
"""Rebuild PUBG montage per user feedback:
#l5Y37N588Ig_m75_195_676 — lengthen part1, drop part2, keep part3.
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
os.chdir(SCRIPTS)


def _load_env() -> None:
    env_path = Path("/root/.video_bot.env")
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:]
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip("\"'"))


def main() -> int:
    _load_env()
    os.environ["SHOOTER_VOD_MONTAGE"] = "1"
    os.environ["PUBG_VOD_MONTAGE"] = "1"
    os.environ["SHOOTER_VOD_EXTEND_HOT"] = "1"
    # Allow a longer first fight so the spray isn't cut mid-exchange.
    os.environ["SHOOTER_VOD_MONTAGE_PART_MAX_SEC"] = "32"
    os.environ["SMART_PUBG_CLIP_MAX_SEC"] = "28"
    os.environ["HIGHLIGHT_WINDOW_SEC"] = "20"
    os.environ["PUBG_VOD_LEAD_SEC"] = "5"

    from shooter_vod_montage import (
        apply_run_trim_to_clip,
        build_montage_id,
        concat_rendered_parts,
    )
    from shooter_vod_segment_feed import (
        keyboard,
        render_single_segment,
        send_message,
        send_video,
        upsert_segment,
        vod_youtube_id,
        _ffprobe_duration,
        _paths,
    )
    from smart_video_editor import ffprobe_duration as probe_dur

    game = "pubg"
    vod = Path("/root/data/pubg/youtube_nightly/inbox/yt_l5Y37N588Ig.mp4")
    if not vod.exists():
        print("missing vod", vod)
        return 2

    token = os.environ.get("TG_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TG_CHAT_ID", "").strip()
    if not token or not chat_id:
        print("missing TG creds")
        return 2

    # Keep peaks 75 + 676; drop 195. Lengthen first via lead+part_max above.
    specs = [
        {"peak": 75.0, "lead": 5.0, "part_max": 32.0, "label": "p1_long"},
        {"peak": 676.0, "lead": 3.0, "part_max": 22.0, "label": "p3_keep"},
    ]

    rows: list[dict] = []
    temps: list[Path] = []
    durs: list[float] = []
    try:
        for spec in specs:
            peak = float(spec["peak"])
            lead = float(spec["lead"])
            part_max = float(spec["part_max"])
            start = max(0.0, peak - lead)
            # Temporarily set part max for this clip's extend/trim.
            os.environ["SHOOTER_VOD_MONTAGE_PART_MAX_SEC"] = str(part_max)
            clip = {
                "start": start,
                "input_duration": min(part_max, lead + 18.0),
                "output_duration": min(part_max, lead + 18.0),
                "peak_start": peak,
            }
            clip = apply_run_trim_to_clip(clip, vod, game=game)
            # If still short on first part, force a longer canvas before peak+after.
            if spec["label"] == "p1_long":
                want = min(part_max, max(float(clip.get("input_duration") or 0), 28.0))
                # Prefer more post-peak: start a bit earlier if needed.
                new_start = max(0.0, peak - 6.0)
                clip["start"] = new_start
                clip["input_duration"] = want
                clip["output_duration"] = want
                clip["peak_start"] = peak
                clip = apply_run_trim_to_clip(clip, vod, game=game)
                # Final floor: at least 26s unless file ends.
                dur_now = float(clip.get("input_duration") or 0)
                if dur_now < 26.0:
                    clip["input_duration"] = min(part_max, 28.0)
                    clip["output_duration"] = float(clip["input_duration"])

            sid = f"{vod_youtube_id(vod)}_{int(float(clip['start']))}"
            part = Path(tempfile.mkstemp(suffix=".part.mp4")[1])
            temps.append(part)
            print(
                "render",
                sid,
                "start",
                clip["start"],
                "dur",
                clip.get("input_duration"),
                "peak",
                peak,
                flush=True,
            )
            if not render_single_segment(vod, clip, part):
                print("render fail", sid)
                return 1
            dur = float(clip.get("input_duration") or 0) or float(probe_dur(part) or 0)
            rows.append(
                {
                    "segment_id": sid,
                    "start": float(clip["start"]),
                    "peak_start": peak,
                    "score": 1.0,
                    "clip": clip,
                    "fight_dur": dur,
                }
            )
            durs.append(dur)

        mid = build_montage_id(vod_youtube_id(vod), rows)
        # Explicit id so feedback maps clearly: peaks 75 + 676 only.
        mid = f"{vod_youtube_id(vod)}_m75_676_fix"
        seg_root = _paths(game)["segments"]
        seg_root.mkdir(parents=True, exist_ok=True)
        out = seg_root / f"seg_{mid}.mp4"
        if not concat_rendered_parts([Path(t) for t in temps], durs, out):
            print("concat fail")
            return 1
        seg_dur = _ffprobe_duration(out)
        peaks = [int(r["peak_start"]) for r in rows]
        caption = (
            f"PUBG Metro склейка #{mid}\n"
            f"🎯 {len(rows)} боя @ {peaks}\n"
            f"{vod_youtube_id(vod)} | {seg_dur:.0f}с\n"
            f"✓ fix: 1-й удлинён, 2-й убран, 3-й как был\n"
            f"👍 Ок / 👎 Не ок"
        )
        print("out", out, "dur", seg_dur, "parts", durs, flush=True)
        # Do not burn an extra daily quota slot — replacement of prior montage.
        ok = send_video(
            token,
            chat_id,
            out,
            caption,
            seg_id=mid,
            record_learning=False,
            reply_markup=keyboard(game, mid),
            cycle_game=None,
        )
        if not ok:
            send_message(token, chat_id, caption + "\n(файл не отправился)")
            print("send fail")
            return 1
        upsert_segment(
            game,
            {
                "segment_id": mid,
                "path": str(out),
                "vod": str(vod),
                "vod_id": vod_youtube_id(vod),
                "start": rows[0]["start"],
                "duration": seg_dur,
                "peak_start": rows[0]["peak_start"],
                "score": 1.0,
                "montage_parts": [r["segment_id"] for r in rows],
                "sig": "feedback_fix_drop195_len75",
                "ingested_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            },
        )
        print("SENT", mid, flush=True)
        return 0
    finally:
        for t in temps:
            try:
                Path(t).unlink(missing_ok=True)
            except Exception:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
