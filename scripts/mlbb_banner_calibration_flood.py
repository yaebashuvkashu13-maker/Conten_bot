#!/usr/bin/env python3
"""Aggressive banner-calibration flood: segment peaks + sparse HUD scan."""

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
from mlbb_banner_calibration_positive_feed import _read_frame, verified_before_send
from mlbb_kill_banner import KillBannerHit, _announce_color_score, _classify_frame, _ffmpeg_sample_frames
from mlbb_telegram_video import send_photo_file
from youtube_download import load_env

ENV_PATH = Path("/root/.video_bot.env")


def _resolve_vod(inbox: Path, vid: str) -> Path | None:
    direct = inbox / f"yt_{vid[:11]}.mp4"
    if direct.exists():
        return direct
    low = vid[:11].lower()
    for path in inbox.glob("yt_*.mp4"):
        if vod_youtube_id(path).lower() == low:
            return path
    return None


def _color_hit(sec: float, frame, color: float) -> KillBannerHit:
    return KillBannerHit(
        sec=round(sec, 2),
        tier=2,
        label="color",
        text=f"color={color:.3f}",
        source="color",
    )


def _collect_segment_peaks(inbox: Path, labeled: dict, sent: set, *, limit: int) -> list[tuple[Path, KillBannerHit, str]]:
    idx_path = Path(os.environ.get("MLBB_VOD_SEGMENT_INDEX", "/root/data/mlbb/vod_segment_index.json"))
    if not idx_path.exists():
        return []
    data = json.loads(idx_path.read_text(encoding="utf-8"))
    rows: list[tuple[Path, KillBannerHit, str]] = []
    seen_vod_sec: set[str] = set()
    for row in data.get("segments", []):
        vod_path = str(row.get("vod") or "")
        vod = Path(vod_path) if vod_path and Path(vod_path).exists() else None
        if vod is None:
            vid = str(row.get("vod_id") or str(row.get("segment_id", "")).rsplit("_", 1)[0])
            vod = _resolve_vod(inbox, vid)
        if vod is None:
            continue
        sec = float(row.get("peak_start") if row.get("peak_start") is not None else row.get("start") or 0)
        dedupe_key = f"{vod_youtube_id(vod)}_{int(sec)}"
        if dedupe_key in seen_vod_sec:
            continue
        seen_vod_sec.add(dedupe_key)
        cid = check_id(vod, sec)
        if cid in labeled or cid in sent:
            continue
        tier = int(row.get("kill_banner_tier") or 0)
        label = str(row.get("kill_banner") or "segment_peak")
        hit = KillBannerHit(
            sec=sec,
            tier=tier if tier > 0 else 3,
            label=label,
            text=str(row.get("pass_reason") or row.get("gate_reason") or "segment_peak")[:80],
            source="segment",
        )
        rows.append((vod, hit, cid))
        if len(rows) >= limit:
            break
    return rows


def _collect_scan_hits(
    inbox: Path,
    labeled: dict,
    sent: set,
    *,
    vod_limit: int,
    samples_per_vod: int,
    limit: int,
) -> list[tuple[Path, KillBannerHit, str]]:
    color_min = float(os.environ.get("MLBB_BANNER_FLOOD_COLOR_MIN", "0.22"))
    t0 = float(os.environ.get("MLBB_BANNER_FLOOD_T0", "60"))
    t1 = float(os.environ.get("MLBB_BANNER_FLOOD_T1", "1500"))
    # Default: skip VODs that already produced labels/sends (old slow path).
    # Teach mode revisits those VODs — only skip exact check_ids already seen.
    teach = os.environ.get("MLBB_BANNER_TEACH_FLOOD", "0") == "1"
    skip_used_vods = (not teach) and os.environ.get("MLBB_BANNER_FLOOD_SKIP_USED_VODS", "1") == "1"
    used_vids = {cid.rsplit("_", 1)[0].lower() for cid in list(labeled) + list(sent)} if skip_used_vods else set()
    vods = sorted(inbox.glob("yt_*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True)
    rows: list[tuple[Path, KillBannerHit, str]] = []
    scanned_vods = 0
    for vod in vods:
        vid = vod_youtube_id(vod).lower()
        if skip_used_vods and vid in used_vids:
            continue
        frames = _ffmpeg_sample_frames(vod, t0, t1, samples_per_vod)
        for sec, frame in frames:
            hit = _classify_frame(sec, frame)
            if hit is None:
                color = _announce_color_score(frame)
                if color < color_min:
                    continue
                hit = _color_hit(sec, frame, color)
            cid = check_id(vod, hit.sec)
            if cid in labeled or cid in sent or cid in {r[2] for r in rows}:
                continue
            rows.append((vod, hit, cid))
            if len(rows) >= limit:
                return rows
        scanned_vods += 1
        if scanned_vods >= vod_limit:
            break
    return rows


def _upgrade_hit_fullres(vod: Path, hit: KillBannerHit) -> KillBannerHit | None:
    """Color/low-res hits must become OCR hits on a full-resolution frame."""
    from mlbb_kill_banner import _ocr_banner_zones, classify_banner_text, _min_tier

    frame = _read_frame(vod, hit.sec)
    if frame is None:
        return None
    text = _ocr_banner_zones(frame, deep=True)
    cls = classify_banner_text(text)
    if cls is None or int(cls.tier) < max(2, _min_tier()):
        return None
    return KillBannerHit(
        sec=round(float(hit.sec), 2),
        tier=int(cls.tier),
        label=str(cls.label),
        text=str(cls.text or "")[:120],
        source="ocr",
    )


def main() -> int:
    env = {**os.environ, **load_env(ENV_PATH)}
    token = env.get("TG_BOT_TOKEN", "")
    chat_id = env.get("TG_CHAT_ID", "")
    if not token or not chat_id:
        print("missing telegram creds")
        return 1

    # Prefer smart OCR teach when available unless explicitly disabled.
    if os.environ.get("MLBB_BANNER_TEACH_FLOOD", "0") == "1" and os.environ.get(
        "MLBB_BANNER_USE_SMART_TEACH", "1"
    ) == "1":
        from mlbb_banner_smart_teach import main as smart_main

        return int(smart_main())

    max_send = int(os.environ.get("MLBB_BANNER_FLOOD_MAX", "40"))
    target = calibration_target()
    st = stats()
    remaining = max(0, target - st["labeled"])
    if remaining > 0:
        max_send = min(max_send, remaining + int(os.environ.get("MLBB_BANNER_FLOOD_EXTRA", "15")))

    inbox = Path(os.environ.get("MLBB_VOD_INBOX", "/root/data/mlbb/youtube_nightly/inbox"))
    labeled = labeled_ids()
    sent = load_sent()

    seg_limit = int(os.environ.get("MLBB_BANNER_FLOOD_SEGMENT_LIMIT", "25"))
    scan_limit = int(os.environ.get("MLBB_BANNER_FLOOD_SCAN_LIMIT", "25"))
    scan_vods = int(os.environ.get("MLBB_BANNER_FLOOD_VODS", "20"))
    samples = int(os.environ.get("MLBB_BANNER_FLOOD_SAMPLES", "10"))
    delay = float(os.environ.get("MLBB_BANNER_FLOOD_DELAY_SEC", "0.35"))
    require_ocr = os.environ.get("MLBB_BANNER_FLOOD_REQUIRE_OCR", "1") == "1"

    sent_n = 0
    seen: set[str] = set()

    def _send_batch(batch: list[tuple[Path, KillBannerHit, str]]) -> int:
        nonlocal sent_n
        n = 0
        for i, (vod, hit, cid) in enumerate(batch, start=sent_n + 1):
            if sent_n >= max_send:
                break
            live_hit = hit
            if require_ocr or live_hit.source in ("color", "segment", "announce"):
                upgraded = _upgrade_hit_fullres(vod, live_hit)
                if upgraded is None:
                    print(f"skip_send {cid}: no_fullres_ocr", flush=True)
                    continue
                live_hit = upgraded
                cid = check_id(vod, live_hit.sec)
                if cid in labeled or cid in load_sent() or cid in seen:
                    continue
            frame = _read_frame(vod, live_hit.sec)
            ok, why = verified_before_send(vod, live_hit, frame)
            if not ok:
                # OCR Double+ may still be blocked by weak no_banner hist — allow when OCR tier>=2
                if not (
                    live_hit.source.startswith("ocr")
                    and int(live_hit.tier) >= 2
                    and "no_banner" in why
                ):
                    print(f"skip_send {cid}: {why}", flush=True)
                    continue
            try:
                shot, meta = render_check_screenshot(vod, live_hit.sec, hit=live_hit)
            except Exception as exc:
                print(f"capture_fail {cid}: {exc}")
                continue
            st = stats()
            caption = (
                f"🎯 Банер {sent_n + 1}/{max_send} | {st['labeled']}/{target}\n"
                f"{meta.get('vod_id', '')} @ {live_hit.sec:.1f}s | {live_hit.label} t{live_hit.tier}\n"
                f"#{cid}"
            )
            if send_photo_file(token, chat_id, shot, caption, reply_markup=inline_keyboard_markup(cid)):
                mark_sent([cid])
                seen.add(cid)
                sent_n += 1
                n += 1
                print(f"sent {cid}", flush=True)
            time.sleep(delay)
        return n

    seg_rows = _collect_segment_peaks(inbox, labeled, sent, limit=seg_limit)
    for item in seg_rows:
        if item[2] in seen:
            continue
        seen.add(item[2])
    _send_batch([r for r in seg_rows if r[2] in seen][:max_send])

    if sent_n < max_send and scan_limit > 0:
        # Stream: send each OCR hit immediately so owner sees panels while scan continues.
        color_min = float(os.environ.get("MLBB_BANNER_FLOOD_COLOR_MIN", "0.35"))
        t0 = float(os.environ.get("MLBB_BANNER_FLOOD_T0", "60"))
        t1 = float(os.environ.get("MLBB_BANNER_FLOOD_T1", "1500"))
        teach = os.environ.get("MLBB_BANNER_TEACH_FLOOD", "0") == "1"
        skip_used_vods = (not teach) and os.environ.get("MLBB_BANNER_FLOOD_SKIP_USED_VODS", "1") == "1"
        used_vids = (
            {cid.rsplit("_", 1)[0].lower() for cid in list(labeled) + list(sent)} if skip_used_vods else set()
        )
        vods = sorted(inbox.glob("yt_*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True)
        scanned_vods = 0
        live_sent = load_sent()
        for vod in vods:
            if sent_n >= max_send:
                break
            vid = vod_youtube_id(vod).lower()
            if skip_used_vods and vid in used_vids:
                continue
            print(f"scan_vod {vid} scanned={scanned_vods} sent={sent_n}", flush=True)
            frames = _ffmpeg_sample_frames(vod, t0, t1, samples)
            batch: list[tuple[Path, KillBannerHit, str]] = []
            for sec, frame in frames:
                hit = _classify_frame(sec, frame)
                if hit is None:
                    # Color is only a HINT to spend full-res OCR — never a send source.
                    color = _announce_color_score(frame)
                    if color < color_min:
                        continue
                    # Probe full-res immediately
                    probe = KillBannerHit(
                        sec=round(sec, 2),
                        tier=2,
                        label="color_probe",
                        text=f"color={color:.3f}",
                        source="color",
                    )
                    upgraded = _upgrade_hit_fullres(vod, probe)
                    if upgraded is None:
                        continue
                    hit = upgraded
                elif hit.source == "color":
                    upgraded = _upgrade_hit_fullres(vod, hit)
                    if upgraded is None:
                        continue
                    hit = upgraded
                cid = check_id(vod, hit.sec)
                if cid in labeled or cid in live_sent or cid in seen:
                    continue
                seen.add(cid)
                batch.append((vod, hit, cid))
            if batch:
                _send_batch(batch)
                live_sent = load_sent()
            scanned_vods += 1
            if scanned_vods >= scan_vods:
                break

    print(json.dumps({"sent": sent_n, "stats": stats()}, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
