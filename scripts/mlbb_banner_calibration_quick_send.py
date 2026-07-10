#!/usr/bin/env python3
"""Fast banner-positive screenshot send: OCR + owner-learned gates only."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mlbb_banner_calibration_capture import render_check_screenshot
from mlbb_banner_calibration_positive_feed import (
    _read_frame,
    _score_candidate,
    positive_candidate_ok,
    verified_before_send,
)
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


def _ocr_hit(sec: float, frame, *, deep: bool = False) -> KillBannerHit | None:
    blob = _ocr_banner_zones(frame, deep=deep)
    row = classify_banner_text(blob)
    if row is None and not deep:
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


def _vod_label_counts() -> dict[str, int]:
    from mlbb_banner_calibration_store import load_labels

    counts: dict[str, int] = {}
    for row in load_labels().get("labels", []):
        name = Path(str(row.get("vod", ""))).name
        if name:
            counts[name] = counts.get(name, 0) + 1
    return counts


def collect_candidates() -> list[tuple[Path, KillBannerHit, str, float]]:
    inbox = Path(os.environ.get("MLBB_VOD_INBOX", "/root/data/mlbb/youtube_nightly/inbox"))
    labeled = labeled_ids()
    batch = int(os.environ.get("MLBB_QUICK_SEND_BATCH", "12"))
    vod_n = int(os.environ.get("MLBB_QUICK_SEND_VODS", "10"))
    step = float(os.environ.get("MLBB_QUICK_SEND_STEP_SEC", "55"))
    t0 = float(os.environ.get("MLBB_QUICK_SEND_T0", "120"))
    t1 = float(os.environ.get("MLBB_QUICK_SEND_T1", "900"))
    min_tier = int(os.environ.get("MLBB_QUICK_SEND_MIN_TIER", "2"))
    min_score = float(os.environ.get("MLBB_QUICK_SEND_MIN_SCORE", "4"))

    label_counts = _vod_label_counts()
    all_vods = sorted(
        [p for p in inbox.glob("yt_*.mp4") if p.is_file() and p.stat().st_size > 500_000],
        key=lambda p: (label_counts.get(p.name, 0), -p.stat().st_mtime),
    )
    vods = all_vods[:vod_n]

    out: list[tuple[Path, KillBannerHit, str, float]] = []
    rejected: dict[str, int] = {}
    for vod in vods:
        sec = t0
        while sec < t1 and len(out) < batch:
            frame = _read_frame(vod, sec)
            if frame is not None:
                hit = _ocr_hit(sec, frame, deep=False)
                if hit is not None and hit.tier >= min_tier:
                    cid = check_id(vod, hit.sec)
                    if cid not in labeled and cid not in {x[2] for x in out}:
                        ok, why = verified_before_send(vod, hit, frame)
                        if not ok:
                            rejected[why] = rejected.get(why, 0) + 1
                        else:
                            score = _score_candidate(hit, frame)
                            if score >= min_score:
                                out.append((vod, hit, cid, score))
            sec += step
        if len(out) >= batch:
            break
    if rejected:
        print(json.dumps({"rejected": rejected}, ensure_ascii=False), flush=True)
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

    if not candidates:
        print(json.dumps({"sent": 0, "stats": stats(), "note": "no_verified_candidates"}, ensure_ascii=False), flush=True)
        return 0

    sent_n = 0
    target = calibration_target()
    for i, (vod, hit, cid, score) in enumerate(candidates, start=1):
        frame = _read_frame(vod, hit.sec)
        ok, why = verified_before_send(vod, hit, frame)
        if not ok:
            print(f"skip_send {cid}: {why}", flush=True)
            continue
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
