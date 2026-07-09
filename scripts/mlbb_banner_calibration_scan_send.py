#!/usr/bin/env python3
"""Scan fresh VOD windows for high-tier banners and send positive cal screenshots."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mlbb_banner_calibration_capture import render_check_screenshot
from mlbb_banner_calibration_positive_feed import _read_frame, _score_candidate
from mlbb_banner_calibration_reasons import inline_keyboard_markup
from mlbb_banner_calibration_store import calibration_target, check_id, labeled_ids, mark_sent, stats
from mlbb_kill_banner import KillBannerHit, _classify_frame, _ffmpeg_sample_frames
from mlbb_telegram_video import send_photo_file
from youtube_download import load_env

ENV_PATH = Path("/root/.video_bot.env")


def main() -> int:
    env = {**os.environ, **load_env(ENV_PATH)}
    token = env.get("TG_BOT_TOKEN", "")
    chat_id = env.get("TG_CHAT_ID", "")
    if not token or not chat_id:
        print("missing telegram creds", flush=True)
        return 1

    os.environ.setdefault("MLBB_BANNER_OWNER_GATE", "0")
    os.environ.setdefault("MLBB_BANNER_NEG_REF_MATCH", "0")

    inbox = Path(os.environ.get("MLBB_VOD_INBOX", "/root/data/mlbb/youtube_nightly/inbox"))
    labeled = labeled_ids()
    batch = int(os.environ.get("MLBB_POS_SCAN_BATCH", "15"))
    min_tier = int(os.environ.get("MLBB_POS_SCAN_MIN_TIER", "3"))
    min_score = float(os.environ.get("MLBB_POS_SCAN_MIN_SCORE", "5"))
    vod_n = int(os.environ.get("MLBB_POS_SCAN_VODS", "6"))
    samples = int(os.environ.get("MLBB_POS_SCAN_SAMPLES", "12"))

    vods = sorted(inbox.glob("yt_*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True)[:vod_n]
    candidates: list[tuple[Path, KillBannerHit, str, float]] = []

    for vod in vods:
        print(f"scan {vod.name}", flush=True)
        frames = _ffmpeg_sample_frames(
            vod,
            float(os.environ.get("MLBB_POS_SCAN_T0", "45")),
            float(os.environ.get("MLBB_POS_SCAN_T1", "900")),
            samples,
        )
        for sec, frame in frames:
            hit = _classify_frame(sec, frame)
            if hit is None or int(hit.tier) < min_tier:
                continue
            cid = check_id(vod, hit.sec)
            if cid in labeled or cid in {c[2] for c in candidates}:
                continue
            score = _score_candidate(hit, frame)
            if score < 0:
                score = float(hit.tier) * 2.0 + (3.0 if hit.source == "ocr" else 1.0)
            if score < min_score:
                continue
            candidates.append((vod, hit, cid, score))
        print(f"  hits_so_far={len(candidates)}", flush=True)

    candidates = sorted(candidates, key=lambda x: -x[3])[:batch]
    print(json.dumps({"candidates": len(candidates)}, ensure_ascii=False), flush=True)

    sent_n = 0
    target = calibration_target()
    for i, (vod, hit, cid, score) in enumerate(candidates, start=1):
        shot, meta = render_check_screenshot(vod, hit.sec, hit=hit)
        st = stats()
        caption = (
            f"✅ Кандидат {i}/{len(candidates)} | {st['labeled']}/{target}\n"
            f"бот: {hit.label} tier={hit.tier} score={score:.1f}\n"
            f"{meta.get('vod_id', '')} @ {hit.sec:.1f}s\n"
            f"#{cid}\n"
            f"Если ок — ✅ Свой kill / 🔥 Savage / ⚡ Double-Triple"
        )
        if send_photo_file(token, chat_id, shot, caption, reply_markup=inline_keyboard_markup(cid)):
            mark_sent([cid])
            sent_n += 1
            print(f"sent {cid}", flush=True)
        time.sleep(0.25)

    print(json.dumps({"sent": sent_n, "stats": stats()}, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
