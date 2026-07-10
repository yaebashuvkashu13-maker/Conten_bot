#!/usr/bin/env python3
"""Scan fresh VOD windows for high-tier banners and send positive cal screenshots."""

from __future__ import annotations

import json
import os
import sys
import time
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mlbb_banner_calibration_capture import render_check_screenshot
from mlbb_banner_calibration_positive_feed import _read_frame, _score_candidate, positive_candidate_ok
from mlbb_banner_calibration_reasons import inline_keyboard_markup
from mlbb_banner_calibration_store import calibration_target, check_id, labeled_ids, mark_sent, stats
from mlbb_kill_banner import KillBannerHit, _classify_frame, _ffmpeg_sample_frames
from mlbb_telegram_video import send_photo_file
from youtube_download import load_env

ENV_PATH = Path("/root/.video_bot.env")
LOCK_PATH = Path(os.environ.get("MLBB_POS_SCAN_LOCK", "/tmp/mlbb_banner_positive_scan.lock"))


def _scan_state_path() -> Path:
    return Path(
        os.environ.get(
            "MLBB_POS_SCAN_STATE",
            "/root/data/mlbb/banner_pos_scan_state.json",
        )
    )


def _pick_vods_rotating(inbox: Path, limit: int) -> list[Path]:
    """Round-robin across inbox so each cron run hits different VODs."""
    all_vods = sorted(
        [p for p in inbox.glob("yt_*.mp4") if p.is_file() and p.stat().st_size > 500_000],
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not all_vods:
        return []
    state_path = _scan_state_path()
    offset = 0
    if state_path.exists():
        try:
            offset = int(json.loads(state_path.read_text(encoding="utf-8")).get("offset", 0))
        except (json.JSONDecodeError, OSError, ValueError, TypeError):
            offset = 0
    offset %= len(all_vods)
    picked = [all_vods[(offset + i) % len(all_vods)] for i in range(min(limit, len(all_vods)))]
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps(
            {
                "offset": (offset + len(picked)) % len(all_vods),
                "last_vods": [p.name for p in picked[:6]],
                "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return picked


@contextmanager
def _singleton_lock():
    acquired = False
    try:
        if LOCK_PATH.exists():
            age = time.time() - LOCK_PATH.stat().st_mtime
            if age < float(os.environ.get("MLBB_POS_SCAN_LOCK_MAX_SEC", "2400")):
                yield False
                return
            LOCK_PATH.unlink(missing_ok=True)
        LOCK_PATH.write_text(str(os.getpid()), encoding="utf-8")
        acquired = True
        yield True
    finally:
        if acquired:
            LOCK_PATH.unlink(missing_ok=True)


def main() -> int:
    with _singleton_lock() as acquired:
        if not acquired:
            print("skip positive scan: another instance running", flush=True)
            return 0
        return _run()


def _run() -> int:
    env = {**os.environ, **load_env(ENV_PATH)}
    token = env.get("TG_BOT_TOKEN", "")
    chat_id = env.get("TG_CHAT_ID", "")
    if not token or not chat_id:
        print("missing telegram creds", flush=True)
        return 1

    inbox = Path(os.environ.get("MLBB_VOD_INBOX", "/root/data/mlbb/youtube_nightly/inbox"))
    labeled = labeled_ids()
    batch = int(os.environ.get("MLBB_POS_SCAN_BATCH", "15"))
    min_tier = int(os.environ.get("MLBB_POS_SCAN_MIN_TIER", "2"))
    min_score = float(os.environ.get("MLBB_POS_SCAN_MIN_SCORE", "4"))
    vod_n = int(os.environ.get("MLBB_POS_SCAN_VODS", "18"))
    samples = int(os.environ.get("MLBB_POS_SCAN_SAMPLES", "8"))

    vods = _pick_vods_rotating(inbox, vod_n)
    candidates: list[tuple[Path, KillBannerHit, str, float]] = []

    for vod in vods:
        print(f"scan {vod.name}", flush=True)
        frames = _ffmpeg_sample_frames(
            vod,
            float(os.environ.get("MLBB_POS_SCAN_T0", "90")),
            float(os.environ.get("MLBB_POS_SCAN_T1", "1200")),
            samples,
        )
        for sec, frame in frames:
            hit = _classify_frame(sec, frame)
            if hit is None or int(hit.tier) < min_tier:
                continue
            if not positive_candidate_ok(hit, frame, vod=vod):
                continue
            cid = check_id(vod, hit.sec)
            if cid in labeled or cid in {c[2] for c in candidates}:
                continue
            score = _score_candidate(hit, frame)
            if score < min_score:
                continue
            candidates.append((vod, hit, cid, score))
        print(f"  hits_so_far={len(candidates)}", flush=True)
        if len(candidates) >= batch:
            break

    candidates = sorted(candidates, key=lambda x: -x[3])[:batch]
    print(json.dumps({"candidates": len(candidates), "vods": [v.name for v in vods[:5]]}, ensure_ascii=False), flush=True)

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
            f"бот: {hit.label} tier={hit.tier} score={score:.1f} src={hit.source}\n"
            f"{meta.get('vod_id', '')} @ {hit.sec:.1f}s\n"
            f"#{cid}\n"
            f"Если ок — ✅ Свой kill / 🔥 Savage / ⚡ Double-Triple"
        )
        if send_photo_file(token, chat_id, shot, caption, reply_markup=inline_keyboard_markup(cid)):
            mark_sent([cid])
            sent_n += 1
            print(f"sent {cid}", flush=True)
        time.sleep(float(os.environ.get("MLBB_POS_SCAN_DELAY_SEC", "0.2")))

    print(json.dumps({"sent": sent_n, "stats": stats()}, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
