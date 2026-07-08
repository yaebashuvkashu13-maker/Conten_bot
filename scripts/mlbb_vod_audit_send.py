#!/usr/bin/env python3
"""Send kill-banner clips from dense audit JSON (owner preview of savage moments)."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mlbb_vod_segment_feed import (  # noqa: E402
    INBOX,
    _ffprobe_duration,
    _normalize_clip,
    file_sha256,
    render_single_segment,
    send_message,
    send_video,
)
from mlbb_vod_segment_store import load_feed_sent, segment_id, segments_root, upsert_segment, vod_youtube_id
from mlbb_vod_title import vod_title_blob
from youtube_download import load_env

ENV_PATH = Path("/root/.video_bot.env")
log = logging.getLogger("mlbb_audit_send")


def _audit_path() -> Path:
    return Path(
        os.environ.get(
            "MLBB_DENSE_AUDIT_JSON",
            "/root/data/mlbb/dense_audit_2026-07-08.json",
        )
    )


def _clip_from_banner(vod: Path, hit: dict) -> dict:
    banner_sec = float(hit["sec"])
    tier = int(hit.get("tier") or 5)
    label = str(hit.get("label") or "savage")
    return {
        "start": banner_sec,
        "peak_start": banner_sec,
        "banner_sec": banner_sec,
        "kill_banner": label,
        "kill_banner_tier": tier,
        "anchor": "kill_banner",
        "highlight_metrics": {"rule_pass": True, "clip_score": 0.5},
    }


def _rows_from_audit(vod: Path, hits: list[dict], sig: str) -> list[dict]:
    sent = load_feed_sent()
    rows: list[dict] = []
    for hit in hits:
        tier = int(hit.get("tier") or 5)
        label = str(hit.get("label") or "savage")
        raw = _clip_from_banner(vod, hit)
        norm = _normalize_clip(raw, vod)
        if norm.get("banner_reject"):
            log.warning("skip banner %.1fs reject=%s", hit["sec"], norm.get("banner_reject"))
            continue
        norm["kill_banner"] = label
        norm["kill_banner_tier"] = tier
        norm["banner_sec"] = float(hit["sec"])
        norm["peak_start"] = float(hit["sec"])
        start = float(norm["start"])
        sid = segment_id(vod, start)
        if sid in sent:
            log.info("skip already sent %s", sid)
            continue
        rows.append(
            {
                "segment_id": sid,
                "start": start,
                "peak_start": float(hit["sec"]),
                "banner_sec": float(hit["sec"]),
                "kill_banner": label,
                "kill_banner_tier": tier,
                "fight_dur": float(norm.get("input_duration") or 0),
                "score": 0.5,
                "hook_score": 0.3,
                "clip_score": 0.5,
                "clip": norm,
                "sig": sig,
            }
        )
    return rows


def send_audit_vod(
    vod: Path,
    hits: list[dict],
    *,
    token: str,
    chat_id: str,
    title_blob: str,
) -> int:
    if not vod.exists():
        log.error("missing vod %s", vod)
        return 0

    os.environ["MLBB_VOD_AUDIT_SEND"] = "1"
    os.environ.pop("MLBB_VOD_TITLE_MIN_TIER", None)
    os.environ["DAILY_GAME_CYCLE_ENABLED"] = "0"

    sig = file_sha256(vod)
    rows = _rows_from_audit(vod, hits, sig)
    if not rows:
        return 0

    vid = vod_youtube_id(vod)
    root = segments_root()
    root.mkdir(parents=True, exist_ok=True)
    sent_n = 0

    for row in rows:
        sid = row["segment_id"]
        peak = int(row["peak_start"])
        label = str(row["kill_banner"]).upper()
        out = root / f"seg_{sid}.mp4"

        if not render_single_segment(vod, row["clip"], out):
            log.warning("render fail %s", sid)
            send_message(token, chat_id, f"❌ {vid} @{peak}s — render fail")
            continue

        if not out.exists() or out.stat().st_size < 100_000:
            log.warning("render too small %s", sid)
            continue

        seg_dur = _ffprobe_duration(out)
        caption = (
            f"🎯 АУДИТ {label} @ {peak}s\n"
            f"{vid} | {seg_dur:.0f}с\n"
            f"{title_blob[:70]}\n"
            f"👍 Ок / 👎 Не ок"
        )
        if send_video(token, chat_id, out, caption, seg_id=sid, record_learning=False):
            upsert_segment(
                {
                    "segment_id": sid,
                    "path": str(out),
                    "vod": str(vod),
                    "vod_id": vid,
                    "start": row["start"],
                    "duration": seg_dur,
                    "peak_start": peak,
                    "kill_banner": row["kill_banner"],
                    "kill_banner_tier": row["kill_banner_tier"],
                    "sig": sig,
                    "ingested_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                }
            )
            sent_n += 1
            log.info("sent audit clip %s peak=%s", sid, peak)
        else:
            log.warning("telegram fail %s size=%s", sid, out.stat().st_size)
            send_message(token, chat_id, f"⚠️ {vid} @{peak}s — видео не ушло (размер {out.stat().st_size // 1024}KB)")
        time.sleep(1.5)

    return sent_n


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Send dense-audit savage moments to Telegram")
    parser.add_argument("--audit", type=Path, default=None)
    parser.add_argument("--vod-id", action="append", default=[], help="Limit to these youtube ids")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    audit_path = args.audit or _audit_path()
    if not audit_path.exists():
        print(f"missing audit json: {audit_path}", file=sys.stderr)
        return 1

    data = json.loads(audit_path.read_text(encoding="utf-8"))
    env = {**os.environ, **load_env(ENV_PATH)}
    token = env.get("TG_BOT_TOKEN", "")
    chat_id = env.get("TG_CHAT_ID", "")
    if not token or not chat_id:
        print("TG missing", file=sys.stderr)
        return 1

    allow = {v.strip() for v in args.vod_id if v.strip()}
    total = 0
    for block in data.get("vods", []):
        vid = str(block.get("vod", "")).replace("yt_", "").replace(".mp4", "")
        if allow and vid not in allow:
            continue
        vod = INBOX / f"yt_{vid}.mp4"
        hits = block.get("banner_times") or []
        if not hits:
            continue
        title_blob = str(block.get("title_blob") or vod_title_blob(vod))
        if args.dry_run:
            print(vid, len(hits), [h["sec"] for h in hits])
            continue
        n = send_audit_vod(vod, hits, token=token, chat_id=chat_id, title_blob=title_blob)
        total += n
        if n:
            send_message(token, chat_id, f"✅ {vid}: отправлено {n} видео из аудита")
        time.sleep(1)

    if not args.dry_run:
        send_message(token, chat_id, f"🏁 Аудит-отправка завершена: {total} видео")
    print(f"audit_send total={total}")
    return 0 if total > 0 or args.dry_run else 1


if __name__ == "__main__":
    raise SystemExit(main())
