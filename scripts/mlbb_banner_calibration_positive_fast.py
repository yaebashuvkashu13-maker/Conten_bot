#!/usr/bin/env python3
"""Fast positive banner candidates: owner-good + high-score segments only."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mlbb_banner_calibration_positive_feed import (
    _read_frame,
    _resolve_vod,
    _score_candidate,
    collect_positive_candidates,
)
from mlbb_banner_calibration_capture import render_check_screenshot
from mlbb_banner_calibration_reasons import inline_keyboard_markup
from mlbb_banner_calibration_store import (
    calibration_target,
    check_id,
    labeled_ids,
    mark_sent,
    stats,
)
from mlbb_kill_banner import KillBannerHit, find_banner_near_peak
from mlbb_telegram_video import send_photo_file
from youtube_download import load_env

ENV_PATH = Path("/root/.video_bot.env")


def _fast_candidates(limit: int) -> list[tuple[Path, KillBannerHit, str, float]]:
    inbox = Path(os.environ.get("MLBB_VOD_INBOX", "/root/data/mlbb/youtube_nightly/inbox"))
    labeled = labeled_ids()
    rows: list[tuple[Path, KillBannerHit, str, float]] = []

    owner = Path(os.environ.get("MLBB_OWNER_LABELS_PATH", "/root/data/mlbb/mobile_legends_owner_labels.json"))
    if owner.exists():
        data = json.loads(owner.read_text(encoding="utf-8"))
        for vid, marks in (data.get("videos") or {}).items():
            vod = _resolve_vod(inbox, vid)
            if vod is None:
                continue
            for mark in marks:
                if str(mark.get("label")) not in ("good", "yes"):
                    continue
                sec = float(mark.get("time_sec") or mark.get("sec") or 0)
                cid = check_id(vod, sec)
                if cid in labeled:
                    continue
                hit = KillBannerHit(sec=sec, tier=5, label="owner_good", text="owner", source="owner")
                frame = _read_frame(vod, hit.sec)
                score = _score_candidate(hit, frame)
                if score >= 0:
                    rows.append((vod, hit, cid, score))

    idx_path = Path(os.environ.get("MLBB_VOD_SEGMENT_INDEX", "/root/data/mlbb/vod_segment_index.json"))
    if idx_path.exists():
        data = json.loads(idx_path.read_text(encoding="utf-8"))
        min_clip = float(os.environ.get("MLBB_POS_CAL_MIN_CLIP", "0.18"))
        for row in data.get("segments", []):
            clip_score = float(row.get("clip_score") or row.get("score") or 0)
            kb = str(row.get("kill_banner") or "").lower()
            if clip_score < min_clip and kb not in ("savage", "legendary", "maniac", "triple"):
                continue
            vod_path = str(row.get("vod") or "")
            vod = Path(vod_path) if vod_path and Path(vod_path).exists() else None
            if vod is None:
                vid = str(row.get("vod_id") or str(row.get("segment_id", "")).rsplit("_", 1)[0])
                vod = _resolve_vod(inbox, vid)
            if vod is None:
                continue
            sec = float(row.get("peak_start") if row.get("peak_start") is not None else row.get("start") or 0)
            cid = check_id(vod, sec)
            if cid in labeled:
                continue
            hit = find_banner_near_peak(vod, sec, quick=True)
            if hit is None:
                tier = 5 if kb in ("savage", "legendary") else 4 if kb == "maniac" else 3
                hit = KillBannerHit(sec=sec, tier=tier, label=kb or "segment", text="segment", source="segment")
            frame = _read_frame(vod, hit.sec)
            score = _score_candidate(hit, frame) + clip_score * 3.0
            if score >= 0:
                rows.append((vod, hit, cid, score))

    merged: dict[str, tuple[Path, KillBannerHit, str, float]] = {}
    for row in rows:
        if row[2] not in merged or row[3] > merged[row[2]][3]:
            merged[row[2]] = row
    return sorted(merged.values(), key=lambda x: -x[3])[:limit]


def main() -> int:
    env = {**os.environ, **load_env(ENV_PATH)}
    token = env.get("TG_BOT_TOKEN", "")
    chat_id = env.get("TG_CHAT_ID", "")
    if not token or not chat_id:
        print("missing telegram creds")
        return 1

    batch = int(os.environ.get("MLBB_POS_CAL_BATCH", "15"))
    use_fast = os.environ.get("MLBB_POS_CAL_FAST", "1") == "1"
    if use_fast:
        candidates = _fast_candidates(batch)
        if len(candidates) < batch // 2:
            extra = collect_positive_candidates(limit=batch)
            seen = {c[2] for c in candidates}
            for row in extra:
                if row[2] not in seen:
                    candidates.append(row)
                    seen.add(row[2])
            candidates = sorted(candidates, key=lambda x: -x[3])[:batch]
    else:
        candidates = collect_positive_candidates(limit=batch)

    print(json.dumps({"candidates": len(candidates), "top": [(c[2], round(c[3], 1)) for c in candidates[:8]]}, ensure_ascii=False))

    sent_n = 0
    target = calibration_target()
    for i, (vod, hit, cid, score) in enumerate(candidates, start=1):
        try:
            shot, meta = render_check_screenshot(vod, hit.sec, hit=hit)
        except Exception as exc:
            print(f"capture_fail {cid}: {exc}")
            continue
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
            print(f"sent {cid} score={score:.1f}")
        time.sleep(float(os.environ.get("MLBB_POS_CAL_DELAY_SEC", "0.25")))

    print(json.dumps({"sent": sent_n, "stats": stats()}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
