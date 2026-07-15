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
    used_vids = {cid.rsplit("_", 1)[0].lower() for cid in list(labeled) + list(sent)}
    vods = sorted(inbox.glob("yt_*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True)
    rows: list[tuple[Path, KillBannerHit, str]] = []
    for vod in vods:
        if vod_youtube_id(vod).lower() in used_vids:
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
        used_vids.add(vod_youtube_id(vod).lower())
        if len(used_vids) >= vod_limit:
            break
    return rows


def main() -> int:
    env = {**os.environ, **load_env(ENV_PATH)}
    token = env.get("TG_BOT_TOKEN", "")
    chat_id = env.get("TG_CHAT_ID", "")
    if not token or not chat_id:
        print("missing telegram creds")
        return 1

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

    sent_n = 0
    seen: set[str] = set()

    def _send_batch(batch: list[tuple[Path, KillBannerHit, str]]) -> int:
        nonlocal sent_n
        n = 0
        teach = os.environ.get("MLBB_BANNER_TEACH_FLOOD", "0") == "1"
        for i, (vod, hit, cid) in enumerate(batch, start=sent_n + 1):
            if sent_n >= max_send:
                break
            frame = _read_frame(vod, hit.sec)
            if not teach:
                ok, why = verified_before_send(vod, hit, frame)
                if not ok:
                    print(f"skip_send {cid}: {why}")
                    continue
            try:
                shot, meta = render_check_screenshot(vod, hit.sec, hit=hit)
            except Exception as exc:
                print(f"capture_fail {cid}: {exc}")
                continue
            st = stats()
            caption = (
                f"🎯 Банер {sent_n + 1}/{max_send} | {st['labeled']}/{target}\n"
                f"{meta.get('vod_id', '')} @ {hit.sec:.1f}s | {hit.label} t{hit.tier}\n"
                f"#{cid}"
            )
            if send_photo_file(token, chat_id, shot, caption, reply_markup=inline_keyboard_markup(cid)):
                mark_sent([cid])
                sent_n += 1
                n += 1
                print(f"sent {cid}")
            time.sleep(delay)
        return n

    seg_rows = _collect_segment_peaks(inbox, labeled, sent, limit=seg_limit)
    for item in seg_rows:
        if item[2] in seen:
            continue
        seen.add(item[2])
    _send_batch([r for r in seg_rows if r[2] in seen][:max_send])

    if sent_n < max_send and int(os.environ.get("MLBB_BANNER_FLOOD_SCAN_LIMIT", "25")) > 0:
        known = set(labeled.keys()) | load_sent()
        scan_rows = _collect_scan_hits(
            inbox,
            {k: labeled[k] for k in known if k in labeled},
            load_sent(),
            vod_limit=scan_vods,
            samples_per_vod=samples,
            limit=min(int(os.environ.get("MLBB_BANNER_FLOOD_SCAN_LIMIT", "25")), max_send - sent_n),
        )
        fresh: list[tuple[Path, KillBannerHit, str]] = []
        for item in scan_rows:
            if item[2] in seen or item[2] in labeled or item[2] in sent:
                continue
            seen.add(item[2])
            fresh.append(item)
        _send_batch(fresh)

    print(json.dumps({"sent": sent_n, "stats": stats()}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
