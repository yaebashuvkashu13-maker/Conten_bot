#!/usr/bin/env python3
"""
Smart owner teach: send ONLY frames with OCR-confirmed kill banners.

Fast path (default):
1. Motion peaks → find_banner_near_peak(quick=True) [already OCR-gated]
2. Full-res shallow OCR confirm (deep only if shallow misses but color hints)
3. Stream Telegram panels immediately (no waiting for full inbox scan)
Never sends color-only / unverified HUD frames.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mlbb_banner_calibration_capture import render_check_screenshot
from mlbb_banner_calibration_reasons import inline_keyboard_markup
from mlbb_banner_calibration_store import (
    calibration_target,
    check_id,
    labeled_ids,
    load_sent,
    mark_sent,
    stats,
    vod_youtube_id,
)
from mlbb_kill_banner import (
    KillBannerHit,
    _announce_color_score,
    _min_tier,
    classify_banner_text,
    find_banner_near_peak,
)
from mlbb_telegram_video import send_photo_file
from youtube_download import load_env

ENV_PATH = Path("/root/.video_bot.env")


def _read_frame(vod: Path, sec: float):
    from gameplay_gate import _read_frame_at

    return _read_frame_at(vod, sec)


def _ocr_confirm(vod: Path, sec: float, *, min_tier: int) -> KillBannerHit | None:
    """Prefer shallow OCR; deepen only when color suggests a banner flash."""
    from mlbb_kill_banner import _ocr_banner_zones

    frame = _read_frame(vod, sec)
    if frame is None:
        return None
    text = _ocr_banner_zones(frame, deep=False)
    hit = classify_banner_text(text)
    if hit is None and _announce_color_score(frame) >= float(
        os.environ.get("MLBB_SMART_TEACH_COLOR_HINT", "0.06")
    ):
        text = _ocr_banner_zones(frame, deep=True)
        hit = classify_banner_text(text)
    if hit is None or int(hit.tier) < min_tier:
        return None
    return KillBannerHit(
        sec=round(float(sec), 2),
        tier=int(hit.tier),
        label=str(hit.label),
        text=str(hit.text or "")[:120],
        source="ocr",
    )


def _peak_hints(vod: Path, *, limit: int) -> list[float]:
    peaks: list[float] = []
    try:
        from mlbb_fight_segment import _analysis_for
        import numpy as np

        analysis = _analysis_for(vod)
        motion = np.asarray(analysis.get("center_motion") or [], dtype=np.float32)
        audio = np.asarray(analysis.get("audio") or [], dtype=np.float32)
        win = float(analysis.get("window_seconds") or 2.0)
        if motion.size == 0:
            return peaks
        combined = motion if audio.size != motion.size else motion * 0.55 + audio * 0.45
        floor = float(os.environ.get("MLBB_SMART_TEACH_PEAK_MIN", "0.10"))
        scored: list[tuple[float, float]] = []
        for i in range(2, len(combined) - 2):
            v = float(combined[i])
            if v < floor:
                continue
            if v >= float(combined[i - 1]) and v >= float(combined[i + 1]):
                scored.append((v, i * win))
        scored.sort(reverse=True)
        return [sec for _, sec in scored[:limit]]
    except Exception:
        pass
    t0 = float(os.environ.get("MLBB_SMART_TEACH_T0", "90"))
    t1 = float(os.environ.get("MLBB_SMART_TEACH_T1", "1200"))
    step = float(os.environ.get("MLBB_SMART_TEACH_FALLBACK_STEP", "120"))
    t = t0
    while t < t1 and len(peaks) < limit:
        peaks.append(t)
        t += step
    return peaks


def _seeds_from_labels(inbox: Path, labeled: dict, sent: set) -> list[tuple[Path, float]]:
    """Revisit a few VODs that already produced owner-positive labels."""
    seeds: list[tuple[Path, float]] = []
    max_seeds = int(os.environ.get("MLBB_SMART_TEACH_SEED_MAX", "24"))
    try:
        from mlbb_banner_calibration_store import load_labels

        rows = [
            row
            for row in load_labels().get("labels", [])
            if str(row.get("reason") or "")
            in {"own_kill_good", "double_triple", "savage_tier", "not_enemy_kill"}
            and not str(row.get("check_id") or "").startswith("ownerphoto")
        ]
        # Prefer recent / higher-tier labels
        rows = list(reversed(rows))[: max_seeds // 2]
        for row in rows:
            vod = Path(str(row.get("vod") or ""))
            if not vod.exists():
                vid = str(row.get("check_id") or "").rsplit("_", 1)[0]
                for path in inbox.glob("yt_*.mp4"):
                    if vod_youtube_id(path).lower() == vid[:11].lower():
                        vod = path
                        break
            if not vod.exists():
                continue
            sec = float(row.get("sec") or 0)
            for delta in (-4.0, 4.0, 10.0):
                seeds.append((vod, max(1.0, sec + delta)))
                if len(seeds) >= max_seeds:
                    return seeds
    except Exception as exc:
        print(f"seed_load_fail: {exc}", flush=True)
    return seeds


def _send_one(
    *,
    token: str,
    chat_id: str,
    vod: Path,
    hit: KillBannerHit,
    cid: str,
    sent_n: int,
    max_send: int,
    target: int,
    score: float,
) -> bool:
    try:
        shot, meta = render_check_screenshot(vod, hit.sec, hit=hit)
    except Exception as exc:
        print(f"capture_fail {cid}: {exc}", flush=True)
        return False
    st = stats()
    caption = (
        f"✅ OCR банер {sent_n + 1}/{max_send} | {st['labeled']}/{target}\n"
        f"{hit.label.upper()} t{hit.tier} score={score:.1f}\n"
        f"{meta.get('vod_id', '')} @ {hit.sec:.1f}s\n"
        f"#{cid}\n"
        f"Жми ✅ Свой kill / ⚡ Double-Triple / 🔥 Savage — или ❌ если ошибка OCR"
    )
    if send_photo_file(token, chat_id, shot, caption, reply_markup=inline_keyboard_markup(cid)):
        mark_sent([cid])
        print(f"sent {cid} {hit.label} t{hit.tier}", flush=True)
        return True
    return False


def main() -> int:
    env = {**os.environ, **load_env(ENV_PATH)}
    token = env.get("TG_BOT_TOKEN", "")
    chat_id = env.get("TG_CHAT_ID", "")
    if not token or not chat_id:
        print("missing telegram creds")
        return 1

    max_send = int(os.environ.get("MLBB_BANNER_FLOOD_MAX", os.environ.get("MLBB_SMART_TEACH_MAX", "12")))
    target = calibration_target()
    min_tier = max(_min_tier(), int(os.environ.get("MLBB_SMART_TEACH_MIN_TIER", str(_min_tier()))))
    inbox = Path(os.environ.get("MLBB_VOD_INBOX", "/root/data/mlbb/youtube_nightly/inbox"))
    labeled = labeled_ids()
    sent = load_sent()
    seen: set[str] = set()
    sent_n = 0
    delay = float(os.environ.get("MLBB_BANNER_FLOOD_DELAY_SEC", "0.25"))
    vod_budget = float(os.environ.get("MLBB_SMART_TEACH_VOD_BUDGET_SEC", "45"))
    global_deadline = time.time() + float(os.environ.get("MLBB_SMART_TEACH_MAX_SEC", "600"))

    print(
        json.dumps(
            {
                "smart_teach_start": True,
                "max_send": max_send,
                "min_tier": min_tier,
                "labeled": len(labeled),
            },
            ensure_ascii=False,
        ),
        flush=True,
    )

    def _try_hit(vod: Path, peak: float) -> KillBannerHit | None:
        hit = find_banner_near_peak(vod, peak, quick=True)
        if hit is None or not str(hit.source).startswith("ocr") or int(hit.tier) < min_tier:
            return None
        return _ocr_confirm(vod, hit.sec, min_tier=min_tier)

    # Pass A: seeds around owner-positive labels (highest ROI)
    seeds = _seeds_from_labels(inbox, labeled, sent)
    print(f"seed_probes={len(seeds)}", flush=True)
    for si, (vod, peak) in enumerate(seeds, start=1):
        if sent_n >= max_send or time.time() > global_deadline:
            break
        if si == 1 or si % 5 == 0:
            print(f"seed {si}/{len(seeds)} {vod_youtube_id(vod)}@{peak:.0f}", flush=True)
        # Direct OCR at seed second — avoid nested find_banner cost
        hit = _ocr_confirm(vod, peak, min_tier=min_tier)
        if hit is None:
            continue
        cid = check_id(vod, hit.sec)
        if cid in labeled or cid in sent or cid in seen:
            continue
        seen.add(cid)
        score = float(hit.tier) * 3.0 + 2.0  # seed bonus
        if _send_one(
            token=token,
            chat_id=chat_id,
            vod=vod,
            hit=hit,
            cid=cid,
            sent_n=sent_n,
            max_send=max_send,
            target=target,
            score=score,
        ):
            sent_n += 1
            sent = load_sent()
            time.sleep(delay)

    # Pass B: newest local VODs — motion peaks + quick OCR finder
    vod_limit = int(os.environ.get("MLBB_SMART_TEACH_VODS", "18"))
    peaks_per = int(os.environ.get("MLBB_SMART_TEACH_PEAKS", "8"))
    vods = sorted(inbox.glob("yt_*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True)
    for i, vod in enumerate(vods[:vod_limit]):
        if sent_n >= max_send or time.time() > global_deadline:
            break
        t0 = time.time()
        print(f"smart_scan {vod_youtube_id(vod)} ({i+1}/{vod_limit}) sent={sent_n}", flush=True)
        for peak in _peak_hints(vod, limit=peaks_per):
            if sent_n >= max_send or time.time() > global_deadline:
                break
            if time.time() - t0 > vod_budget:
                print(f"  budget_skip {vod_youtube_id(vod)}", flush=True)
                break
            hit = _try_hit(vod, peak)
            if hit is None:
                continue
            cid = check_id(vod, hit.sec)
            if cid in labeled or cid in sent or cid in seen:
                continue
            seen.add(cid)
            score = float(hit.tier) * 3.0
            if str(hit.label).lower() in ("savage", "legendary", "maniac"):
                score += 3.0
            if _send_one(
                token=token,
                chat_id=chat_id,
                vod=vod,
                hit=hit,
                cid=cid,
                sent_n=sent_n,
                max_send=max_send,
                target=target,
                score=score,
            ):
                sent_n += 1
                sent = load_sent()
                time.sleep(delay)

    print(json.dumps({"sent": sent_n, "stats": stats()}, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
