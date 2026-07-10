#!/usr/bin/env python3
"""Fast banner-positive screenshot send: sparse frame reads + OCR only."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mlbb_banner_calibration_capture import render_check_screenshot
from mlbb_banner_calibration_positive_feed import positive_candidate_ok, _score_candidate
from mlbb_banner_calibration_reasons import inline_keyboard_markup
from mlbb_banner_calibration_store import (
    calibration_target,
    check_id,
    labeled_ids,
    mark_sent,
    stats,
)
from mlbb_kill_banner import KillBannerHit, classify_banner_text, _ocr_banner_zones
from mlbb_telegram_video import send_photo_file
from youtube_download import load_env

ENV_PATH = Path("/root/.video_bot.env")


def _read_frame(vod: Path, sec: float):
    from gameplay_gate import _read_frame_at

    return _read_frame_at(vod, sec)


def _ocr_hit(sec: float, frame) -> KillBannerHit | None:
    blob = _ocr_banner_zones(frame, deep=True)
    row = classify_banner_text(blob)
    if row is None:
        return None
    return KillBannerHit(
        sec=round(sec, 2),
        tier=row.tier,
        label=row.label,
        text=row.text,
        source="ocr",
    )


def collect_candidates() -> list[tuple[Path, KillBannerHit, str, float]]:
    inbox = Path(os.environ.get("MLBB_VOD_INBOX", "/root/data/mlbb/youtube_nightly/inbox"))
    labeled = labeled_ids()
    batch = int(os.environ.get("MLBB_QUICK_SEND_BATCH", "15"))
    vod_n = int(os.environ.get("MLBB_QUICK_SEND_VODS", "25"))
    step = float(os.environ.get("MLBB_QUICK_SEND_STEP_SEC", "75"))
    t0 = float(os.environ.get("MLBB_QUICK_SEND_T0", "90"))
    t1 = float(os.environ.get("MLBB_QUICK_SEND_T1", "2100"))

    state_path = Path(os.environ.get("MLBB_QUICK_SEND_STATE", "/root/data/mlbb/banner_quick_send_state.json"))
    offset = 0
    if state_path.exists():
        try:
            offset = int(json.loads(state_path.read_text(encoding="utf-8")).get("offset", 0))
        except (json.JSONDecodeError, OSError, ValueError, TypeError):
            offset = 0

    all_vods = sorted(
        [p for p in inbox.glob("yt_*.mp4") if p.is_file() and p.stat().st_size > 500_000],
        key=lambda p: p.stat().st_mtime,
    )
    if not all_vods:
        return []
    offset %= len(all_vods)
    vods = [all_vods[(offset + i) % len(all_vods)] for i in range(min(vod_n, len(all_vods)))]
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps({"offset": (offset + len(vods)) % len(all_vods), "vods": [v.name for v in vods[:5]]}),
        encoding="utf-8",
    )

    out: list[tuple[Path, KillBannerHit, str, float]] = []
    for vod in vods:
        sec = t0
        while sec < t1 and len(out) < batch:
            frame = _read_frame(vod, sec)
            if frame is not None:
                hit = _ocr_hit(sec, frame)
                if hit is not None and hit.tier >= int(os.environ.get("MLBB_QUICK_SEND_MIN_TIER", "2")):
                    cid = check_id(vod, hit.sec)
                    if cid not in labeled and cid not in {x[2] for x in out}:
                        if positive_candidate_ok(hit, frame, vod=vod):
                            score = _score_candidate(hit, frame)
                            if score >= float(os.environ.get("MLBB_QUICK_SEND_MIN_SCORE", "3")):
                                out.append((vod, hit, cid, score))
            sec += step
        if len(out) >= batch:
            break
    return sorted(out, key=lambda x: -x[3])[:batch]


def main() -> int:
    env = {**os.environ, **load_env(ENV_PATH)}
    token = env.get("TG_BOT_TOKEN", "")
    chat_id = env.get("TG_CHAT_ID", "")
    if not token or not chat_id:
        print("missing telegram creds")
        return 1

    candidates = collect_candidates()
    print(json.dumps({"candidates": len(candidates), "ids": [c[2] for c in candidates[:8]]}, ensure_ascii=False), flush=True)

    sent_n = 0
    target = calibration_target()
    for i, (vod, hit, cid, score) in enumerate(candidates, start=1):
        try:
            shot, meta = render_check_screenshot(vod, hit.sec, hit=hit)
        except Exception as exc:
            print(f"capture_fail {cid}: {exc}", flush=True)
            continue
        st = stats()
        caption = (
            f"✅ Кандидат {i}/{len(candidates)} | {st['labeled']}/{target}\n"
            f"бот: {hit.label} tier={hit.tier} src={hit.source} score={score:.1f}\n"
            f"{meta.get('vod_id', '')} @ {hit.sec:.1f}s\n"
            f"#{cid}\n"
            f"Если ок — ✅ Свой kill / 🔥 Savage / ⚡ Double-Triple"
        )
        if send_photo_file(token, chat_id, shot, caption, reply_markup=inline_keyboard_markup(cid)):
            mark_sent([cid])
            sent_n += 1
            print(f"sent {cid}", flush=True)
        time.sleep(float(os.environ.get("MLBB_QUICK_SEND_DELAY_SEC", "0.2")))

    print(json.dumps({"sent": sent_n, "stats": stats()}, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
