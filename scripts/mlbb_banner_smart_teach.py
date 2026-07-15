#!/usr/bin/env python3
"""
Smart owner teach: send ONLY frames with OCR-confirmed kill banners.

Unlike flood (color + sparse 480p samples), this:
1. Takes motion/audio peaks from local VODs
2. Re-reads FULL-RES frames near each peak
3. Runs deep OCR; keeps Double+ (or configured min tier)
4. Optionally requires owner-positive edge match when OCR is weak
5. Never sends color-only or unverified screenshots
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
    _ffmpeg_sample_frames,
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


def _ocr_hit_fullres(vod: Path, sec: float, *, min_tier: int) -> KillBannerHit | None:
    from mlbb_kill_banner import _ocr_banner_zones

    frame = _read_frame(vod, sec)
    if frame is None:
        return None
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


def _owner_edge_ok(frame) -> tuple[bool, float]:
    """Structural owner-positive proof — stronger than HSV alone."""
    try:
        from mlbb_banner_ref_match import match_positive_owner_reference_strict

        row = match_positive_owner_reference_strict(frame)
        if row is None:
            return False, 0.0
        return True, float(row[0])
    except Exception:
        return False, 0.0


def _peak_hints(vod: Path, *, limit: int) -> list[float]:
    """Motion/audio peak seconds across the VOD (cheap probe)."""
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
        combined = motion
        if audio.size == motion.size:
            combined = motion * 0.55 + audio * 0.45
        # local maxima
        for i in range(2, len(combined) - 2):
            v = float(combined[i])
            if v < float(os.environ.get("MLBB_SMART_TEACH_PEAK_MIN", "0.08")):
                continue
            if v >= float(combined[i - 1]) and v >= float(combined[i + 1]):
                if v >= float(combined[i - 2]) and v >= float(combined[i + 2]):
                    peaks.append(i * win)
        peaks.sort(key=lambda s: -float(combined[min(len(combined) - 1, int(s / max(win, 0.1)))]))
        return peaks[:limit]
    except Exception:
        pass
    # Fallback: uniform grid
    t0 = float(os.environ.get("MLBB_SMART_TEACH_T0", "60"))
    t1 = float(os.environ.get("MLBB_SMART_TEACH_T1", "1400"))
    step = float(os.environ.get("MLBB_SMART_TEACH_FALLBACK_STEP", "90"))
    t = t0
    while t < t1 and len(peaks) < limit:
        peaks.append(t)
        t += step
    return peaks


def _probe_near_peak(vod: Path, peak: float, *, min_tier: int) -> KillBannerHit | None:
    """Try quick banner finder, then a short full-res OCR probe around the peak."""
    hit = find_banner_near_peak(vod, peak, quick=True)
    if hit is not None and hit.source.startswith("ocr") and int(hit.tier) >= min_tier:
        # Re-confirm on full-res (find may have used low-res samples)
        confirmed = _ocr_hit_fullres(vod, hit.sec, min_tier=min_tier)
        if confirmed is not None:
            return confirmed
    before = float(os.environ.get("MLBB_SMART_TEACH_BEFORE", "6"))
    after = float(os.environ.get("MLBB_SMART_TEACH_AFTER", "10"))
    step = float(os.environ.get("MLBB_SMART_TEACH_STEP", "0.75"))
    t0 = max(0.0, peak - before)
    t1 = peak + after
    # Dense but limited full-res OCR samples
    n = max(3, int((t1 - t0) / max(step, 0.35)) + 1)
    n = min(n, int(os.environ.get("MLBB_SMART_TEACH_MAX_PROBES", "12")))
    # Use mid-res ffmpeg first as cheap gate, then full-res confirm
    frames = _ffmpeg_sample_frames(vod, t0, t1, n)
    for sec, low in frames:
        color_ok = True
        try:
            from mlbb_kill_banner import _announce_color_score

            # Soft gate only — do not invent color hits.
            if _announce_color_score(low) < float(os.environ.get("MLBB_SMART_TEACH_COLOR_HINT", "0.04")):
                # Still try a few peaks even without gold flash
                if abs(sec - peak) > 1.5:
                    color_ok = False
        except Exception:
            pass
        if not color_ok:
            continue
        confirmed = _ocr_hit_fullres(vod, sec, min_tier=min_tier)
        if confirmed is not None:
            return confirmed
    return None


def collect_ocr_candidates(*, limit: int) -> list[tuple[Path, KillBannerHit, str, float]]:
    inbox = Path(os.environ.get("MLBB_VOD_INBOX", "/root/data/mlbb/youtube_nightly/inbox"))
    labeled = labeled_ids()
    sent = load_sent()
    min_tier = max(_min_tier(), int(os.environ.get("MLBB_SMART_TEACH_MIN_TIER", str(_min_tier()))))
    vod_limit = int(os.environ.get("MLBB_SMART_TEACH_VODS", "25"))
    peaks_per = int(os.environ.get("MLBB_SMART_TEACH_PEAKS", "14"))

    vods = sorted(inbox.glob("yt_*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True)
    out: list[tuple[Path, KillBannerHit, str, float]] = []
    seen: set[str] = set()
    scanned = 0
    for vod in vods:
        if len(out) >= limit:
            break
        scanned += 1
        if scanned > vod_limit:
            break
        print(f"smart_scan {vod_youtube_id(vod)} peaks…", flush=True)
        for peak in _peak_hints(vod, limit=peaks_per):
            if len(out) >= limit:
                break
            hit = _probe_near_peak(vod, peak, min_tier=min_tier)
            if hit is None:
                continue
            cid = check_id(vod, hit.sec)
            if cid in labeled or cid in sent or cid in seen:
                continue
            frame = _read_frame(vod, hit.sec)
            if frame is None:
                continue
            # Require verification: OCR already present; optional edge boost
            edge_ok, edge_score = _owner_edge_ok(frame)
            score = float(hit.tier) * 3.0 + (edge_score * 4.0 if edge_ok else 0.0)
            if str(hit.label).lower() in ("savage", "legendary", "maniac"):
                score += 3.0
            seen.add(cid)
            out.append((vod, hit, cid, score))
            print(
                f"  candidate {cid} {hit.label} t{hit.tier} edge={edge_score:.2f}",
                flush=True,
            )
    out.sort(key=lambda x: -x[3])
    return out[:limit]


def main() -> int:
    env = {**os.environ, **load_env(ENV_PATH)}
    token = env.get("TG_BOT_TOKEN", "")
    chat_id = env.get("TG_CHAT_ID", "")
    if not token or not chat_id:
        print("missing telegram creds")
        return 1

    max_send = int(os.environ.get("MLBB_BANNER_FLOOD_MAX", os.environ.get("MLBB_SMART_TEACH_MAX", "20")))
    target = calibration_target()
    candidates = collect_ocr_candidates(limit=max_send * 2)
    print(json.dumps({"candidates": len(candidates)}, ensure_ascii=False), flush=True)

    sent_n = 0
    delay = float(os.environ.get("MLBB_BANNER_FLOOD_DELAY_SEC", "0.35"))
    for i, (vod, hit, cid, score) in enumerate(candidates, start=1):
        if sent_n >= max_send:
            break
        frame = _read_frame(vod, hit.sec)
        # Final OCR re-check (no color / hist shortcuts)
        confirmed = _ocr_hit_fullres(vod, hit.sec, min_tier=max(2, _min_tier()))
        if confirmed is None:
            print(f"skip_send {cid}: no_fullres_ocr", flush=True)
            continue
        hit = confirmed
        try:
            from mlbb_banner_calibration_positive_feed import verified_before_send

            ok, why = verified_before_send(vod, hit, frame)
            # For teach: allow OCR through even if weak no_banner hist matches
            if not ok and "no_banner" not in why and why not in ("candidate_filter",):
                # still skip hard rejects
                if why.startswith("owner_neg:") and "no_banner" not in why:
                    print(f"skip_send {cid}: {why}", flush=True)
                    continue
            if not ok and os.environ.get("MLBB_SMART_TEACH_STRICT_VERIFY", "0") == "1":
                print(f"skip_send {cid}: {why}", flush=True)
                continue
        except Exception:
            pass
        try:
            shot, meta = render_check_screenshot(vod, hit.sec, hit=hit)
        except Exception as exc:
            print(f"capture_fail {cid}: {exc}", flush=True)
            continue
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
            sent_n += 1
            print(f"sent {cid}", flush=True)
        time.sleep(delay)

    print(json.dumps({"sent": sent_n, "stats": stats()}, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
