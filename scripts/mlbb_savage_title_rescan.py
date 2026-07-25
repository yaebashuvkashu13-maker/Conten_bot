#!/usr/bin/env python3
"""
Rescan Savage/Maniac-titled MLBB VODs, cut 4–5 kill banners, send to Telegram.

Designed for ops + future autonomy: title queue → dense banner discover → send.
Does not burn daily MLBB quota (uses MLBB_VOD_IGNORE_DAILY_QUOTA=1).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

log = logging.getLogger("mlbb_savage_title_rescan")


def _load_env() -> dict[str, str]:
    from smart_video_editor import load_env

    return load_env(Path("/root/.video_bot.env"))


def _queue_path() -> Path:
    return Path(os.environ.get("MLBB_TITLE_RESCAN_QUEUE", "/root/data/mlbb/title_rescan_queue.json"))


def _inbox() -> Path:
    return Path(os.environ.get("MLBB_VOD_INBOX", "/root/data/mlbb/youtube_nightly/inbox"))


def _out_dir() -> Path:
    p = Path(os.environ.get("MLBB_SAVAGE_RESCAN_OUT", "/root/data/mlbb/savage_rescan"))
    p.mkdir(parents=True, exist_ok=True)
    return p


def _sent_registry_path() -> Path:
    return Path(
        os.environ.get(
            "MLBB_SAVAGE_SENT_REGISTRY",
            str(Path(os.environ.get("MLBB_DATA_ROOT", "/root/data/mlbb")) / "savage_title_sent.json"),
        )
    )


def _send_dedup_gap_sec() -> float:
    return max(15.0, float(os.environ.get("MLBB_SAVAGE_SEND_DEDUP_SEC", "45")))


def _load_sent_registry() -> dict:
    path = _sent_registry_path()
    if not path.exists():
        return {"clips": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"clips": []}
    if not isinstance(data, dict):
        return {"clips": []}
    clips = data.get("clips")
    if not isinstance(clips, list):
        data["clips"] = []
    return data


def _save_sent_registry(data: dict) -> None:
    path = _sent_registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _already_sent_near(registry: dict, video_id: str, sec: float, *, gap: float | None = None) -> bool:
    gap = _send_dedup_gap_sec() if gap is None else gap
    for row in registry.get("clips") or []:
        if not isinstance(row, dict):
            continue
        if str(row.get("id") or "") != video_id:
            continue
        try:
            prev = float(row.get("sec") or -999)
        except (TypeError, ValueError):
            continue
        if abs(prev - sec) < gap:
            return True
    return False


def _mark_sent(registry: dict, *, video_id: str, sec: float, label: str, source: str, file: str) -> None:
    clips = registry.setdefault("clips", [])
    clips.append(
        {
            "id": video_id,
            "sec": round(float(sec), 2),
            "label": label,
            "source": source,
            "file": file,
            "at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
    )
    # Keep registry bounded.
    if len(clips) > 500:
        registry["clips"] = clips[-500:]
    _save_sent_registry(registry)


def _load_queue() -> list[dict]:
    path = _queue_path()
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    rows = data.get("queued") if isinstance(data, dict) else data
    return [r for r in rows if isinstance(r, dict) and r.get("id")]


def _ensure_vod(video_id: str, path_hint: str | None = None) -> Path | None:
    from video_frame_io import ensure_h264_mp4

    inbox = _inbox()
    inbox.mkdir(parents=True, exist_ok=True)
    candidates = [
        Path(path_hint) if path_hint else None,
        inbox / f"yt_{video_id}.mp4",
    ]
    for p in candidates:
        if p and p.is_file() and p.stat().st_size > 2_000_000:
            return ensure_h264_mp4(p)
    url = f"https://www.youtube.com/watch?v={video_id}"
    out_tmpl = str(inbox / f"yt_{video_id}.%(ext)s")
    # Prefer AVC1 so OpenCV/dense OCR do not stall on AV1.
    fmt = os.environ.get(
        "YOUTUBE_FORMAT",
        "bv*[height<=720][vcodec^=avc1]+ba/b[height<=720][vcodec^=avc1]/"
        "bv*[height<=720]+ba/b[height<=720]/b",
    )
    cmd = [
        "yt-dlp",
        "-f",
        fmt,
        "--merge-output-format",
        "mp4",
        "-o",
        out_tmpl,
        url,
    ]
    log.info("download %s", video_id)
    proc = subprocess.run(cmd, check=False, timeout=1800)
    if proc.returncode != 0:
        log.warning("download failed id=%s code=%s", video_id, proc.returncode)
        return None
    final = inbox / f"yt_{video_id}.mp4"
    if not final.is_file():
        return None
    return ensure_h264_mp4(final)


def _ffprobe_duration(vod: Path) -> float:
    proc = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(vod),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    try:
        return float((proc.stdout or "0").strip())
    except ValueError:
        return 0.0


def _render_clip(vod: Path, start: float, dur: float, out: Path) -> bool:
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        f"{max(0.0, start):.3f}",
        "-i",
        str(vod),
        "-t",
        f"{dur:.3f}",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "23",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-movflags",
        "+faststart",
        str(out),
    ]
    return subprocess.run(cmd, check=False, timeout=300).returncode == 0


def _prepare_env_for_scan(title: str, dur: float, tier_need: int, *, dense: bool) -> None:
    os.environ["MLBB_VOD_BANNER_DISCOVER"] = "1"
    os.environ["MLBB_VOD_KILL_BANNER"] = "1"
    os.environ["MLBB_VOD_SCAN_TITLE"] = title
    os.environ["MLBB_VOD_TITLE_MIN_TIER"] = str(max(4, tier_need))
    os.environ["MLBB_KILL_BANNER_MIN_TIER"] = "maniac"
    os.environ["MLBB_VOD_IGNORE_DAILY_QUOTA"] = "1"
    # Title-promised streaks: OCR is often blind on YouTube compressions.
    # Soften live owner-pos ref floor so cross-VOD banks can still hit maniac/savage.
    os.environ["MLBB_BANNER_REF_MATCH"] = "1"
    os.environ.setdefault("MLBB_BANNER_POS_LIVE_MIN_SIM", "0.48")
    os.environ.setdefault("MLBB_BANNER_POS_SAVAGE_MIN_SIM", "0.45")
    os.environ.setdefault("MLBB_BANNER_REF_COLOR_MUL", "0.85")
    # Long pre-roll so clips start before the final kill, not on it.
    os.environ.setdefault("MLBB_KILL_BANNER_LEAD_SEC", "16")
    os.environ.setdefault("MLBB_MANIAC_BANNER_LEAD_SEC", "18")
    os.environ.setdefault("MLBB_SAVAGE_BANNER_LEAD_SEC", "22")
    os.environ.setdefault("MLBB_SAVAGE_CLIP_SEC", "30")
    if dense:
        os.environ["MLBB_VOD_BANNER_DENSE_SEC"] = "1"
        # Ops rescans should not inherit the live-feed 5s step.
        os.environ["MLBB_KILL_BANNER_DISCOVER_STEP"] = "1"
        os.environ["MLBB_KILL_BANNER_DISCOVER_MAX_PROBES"] = str(max(180, int(dur) + 40))
        # Dense + OCR is slow; give title rescans a real wall budget.
        os.environ["MLBB_KILL_BANNER_DISCOVER_MAX_SEC"] = str(
            max(480.0, min(1200.0, dur * 2.0 + 120.0))
        )
        # OCR on YT compressions is usually garbage — prefer ref; sparse OCR only.
        os.environ.setdefault("MLBB_KILL_BANNER_TITLE_OCR_EVERY", "8")
    else:
        os.environ["MLBB_VOD_BANNER_DENSE_SEC"] = "0"
        # Fast path: denser spike/OCR budget without 1 Hz full sweep.
        os.environ["MLBB_KILL_BANNER_DISCOVER_MAX_PROBES"] = str(
            max(48, int(os.environ.get("MLBB_SAVAGE_SPIKE_PROBES", "64")))
        )
        os.environ["MLBB_KILL_BANNER_DISCOVER_SPIKE_CAP"] = "40"
        os.environ["MLBB_KILL_BANNER_DISCOVER_OCR_SPIKES"] = "24"
        os.environ["MLBB_KILL_BANNER_DISCOVER_MAX_SEC"] = str(max(180.0, min(420.0, dur * 0.5)))
        os.environ["MLBB_KILL_BANNER_DISCOVER_TARGET"] = "6"


def scan_and_send(
    *,
    limit_vods: int,
    max_clips: int,
    dry_run: bool,
    ids: list[str] | None,
) -> int:
    from mlbb_kill_banner import discover_vod_kill_banners
    from mlbb_vod_title import title_min_banner_tier, vod_title_blob
    from mlbb_telegram_video import send_video_file, send_document_file, TELEGRAM_MAX_BYTES
    from mlbb_fight_segment import banner_lead_sec
    from smart_video_editor import load_env

    env = load_env(Path("/root/.video_bot.env"))
    for k, v in env.items():
        os.environ.setdefault(k, v)
    # Title rescan is an ops/manual path — never block on daily MLBB quota.
    os.environ["DAILY_GAME_CYCLE_ENABLED"] = "0"
    os.environ["MLBB_VOD_IGNORE_DAILY_QUOTA"] = "1"

    token = env.get("TG_BOT_TOKEN", "").strip()
    chat_id = (env.get("TG_CHAT_ID") or env.get("MLBB_CHAT_ID") or "").strip()
    if not dry_run and (not token or not chat_id):
        log.error("TG_BOT_TOKEN / TG_CHAT_ID missing")
        return 2

    queue = _load_queue()
    if ids:
        idset = {x.strip() for x in ids if x.strip()}
        queue = [r for r in queue if str(r.get("id")) in idset]
        # Allow explicit ids missing from queue.
        have = {str(r.get("id")) for r in queue}
        for vid in idset - have:
            queue.append({"id": vid, "title": vid, "tier_need": 4})
    # Prefer shorter VODs first.
    queue.sort(key=lambda r: float(r.get("duration_min") or 99))
    queue = queue[: max(1, limit_vods)]

    sent = 0
    report: list[dict] = []
    tail = float(os.environ.get("MLBB_KILL_BANNER_TAIL_SEC", "8"))
    # Long enough to fit savage pre-roll (prior kills) + banner + short aftermath.
    clip_dur = float(os.environ.get("MLBB_SAVAGE_CLIP_SEC", "28"))
    dedup_gap = _send_dedup_gap_sec()
    sent_reg = _load_sent_registry()

    for row in queue:
        if sent >= max_clips:
            break
        vid = str(row["id"])
        title = str(row.get("title") or vid)
        tier_need = int(row.get("tier_need") or title_min_banner_tier(title.lower()) or 4)
        vod = _ensure_vod(vid, str(row.get("path") or "") or None)
        if vod is None:
            report.append({"id": vid, "error": "download_failed"})
            continue
        dur = _ffprobe_duration(vod)
        blob = vod_title_blob(vod, {"title": title})
        need = max(4, title_min_banner_tier(blob) or tier_need)
        log.info("scan vod=%s need_tier=%s dur=%.0fs title=%s", vod.name, need, dur, title[:70])
        t0 = time.monotonic()
        _prepare_env_for_scan(title, dur, tier_need, dense=False)
        hits = discover_vod_kill_banners(vod, min_tier=need)
        high = [h for h in hits if h.tier >= need]
        if not high and os.environ.get("MLBB_SAVAGE_DENSE_FALLBACK", "1") == "1":
            log.info("spike miss vod=%s — dense fallback", vod.name)
            _prepare_env_for_scan(title, dur, tier_need, dense=True)
            hits = discover_vod_kill_banners(vod, min_tier=need)
            high = [h for h in hits if h.tier >= need]
        # Prefer OCR-confirmed banners; ref-only is allowed as fallback.
        ocr_high = [h for h in high if str(h.source).startswith("ocr")]
        pool = ocr_high or high
        # Dedup near-duplicate banners within this run + across prior rescans.
        kept = []
        for h in sorted(pool, key=lambda x: (-x.tier, 0 if str(x.source).startswith("ocr") else 1, x.sec)):
            if any(abs(h.sec - k.sec) < dedup_gap for k in kept):
                continue
            if _already_sent_near(sent_reg, vid, h.sec, gap=dedup_gap):
                log.info("skip already-sent near vod=%s sec=%.1f gap=%.0fs", vid, h.sec, dedup_gap)
                continue
            kept.append(h)
        log.info(
            "discover vod=%s hits=%s high=%s kept=%s elapsed=%.0fs",
            vod.name,
            len(hits),
            len(high),
            len(kept),
            time.monotonic() - t0,
        )
        entry = {
            "id": vid,
            "title": title[:120],
            "need": need,
            "hits": [{"sec": h.sec, "tier": h.tier, "label": h.label, "source": h.source} for h in kept],
            "sent": [],
        }
        for h in kept:
            if sent >= max_clips:
                break
            if _already_sent_near(sent_reg, vid, h.sec, gap=dedup_gap):
                continue
            lead = banner_lead_sec(h.tier)
            start = max(0.0, h.sec - lead)
            dur_clip = max(clip_dur, lead + tail)
            out = _out_dir() / f"yt_{vid}_{h.label}_{int(h.sec)}.mp4"
            if not _render_clip(vod, start, dur_clip, out):
                log.warning("render fail %s @%.1f", vid, h.sec)
                continue
            caption = (
                f"MLBB {h.label.upper()} (tier {h.tier}) @ {h.sec:.0f}s\n"
                f"pre-roll {lead:.0f}s before banner\n"
                f"{title[:80]}\n"
                f"id={vid} source={h.source} (title rescan)"
            )
            if dry_run:
                log.info("dry-run would send %s", out.name)
                entry["sent"].append({"file": out.name, "dry_run": True})
                sent += 1
                continue
            ok = False
            try:
                if out.stat().st_size <= TELEGRAM_MAX_BYTES:
                    ok = bool(send_video_file(token, chat_id, out, caption))
                else:
                    ok = bool(send_document_file(token, chat_id, out, caption))
            except Exception as exc:
                log.warning("telegram send failed %s: %s", out.name, exc)
            log.info("send %s ok=%s", out.name, ok)
            if ok:
                entry["sent"].append({"file": out.name, "sec": h.sec, "tier": h.tier, "label": h.label})
                _mark_sent(
                    sent_reg,
                    video_id=vid,
                    sec=h.sec,
                    label=h.label,
                    source=h.source,
                    file=out.name,
                )
                sent += 1
        report.append(entry)

    out_json = _out_dir() / f"rescan_{time.strftime('%Y%m%d_%H%M%S')}.json"
    out_json.write_text(json.dumps({"sent": sent, "vods": report}, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("done sent=%s report=%s", sent, out_json)
    return 0 if sent > 0 or dry_run else 1


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit-vods", type=int, default=4)
    ap.add_argument("--max-clips", type=int, default=8)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--id", action="append", default=[], help="YouTube id (repeatable)")
    args = ap.parse_args()
    return scan_and_send(
        limit_vods=args.limit_vods,
        max_clips=args.max_clips,
        dry_run=args.dry_run,
        ids=args.id or None,
    )


if __name__ == "__main__":
    raise SystemExit(main())
