#!/usr/bin/env python3
"""Fast banner-calibration burst: audit hints + owner good labels (no full VOD scan)."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mlbb_banner_calibration_capture import render_check_screenshot
from mlbb_banner_calibration_reasons import inline_keyboard_markup
from mlbb_banner_calibration_store import (
    check_id,
    labeled_ids,
    load_sent,
    mark_sent,
    stats,
    vod_youtube_id,
)
from mlbb_kill_banner import KillBannerHit
from mlbb_telegram_video import send_photo_file
from youtube_download import load_env

ENV_PATH = Path("/root/.video_bot.env")
BATCH = int(os.environ.get("MLBB_BANNER_CALIB_BATCH", "10"))


def _resolve_vod(inbox: Path, vid: str) -> Path | None:
    direct = inbox / f"yt_{vid[:11]}.mp4"
    if direct.exists():
        return direct
    low = vid[:11].lower()
    for path in inbox.glob("yt_*.mp4"):
        if vod_youtube_id(path).lower() == low:
            return path
    return None


def _audit_rows(inbox: Path, labeled: dict, sent: set) -> list[tuple[Path, float, dict, str]]:
    path = Path(os.environ.get("MLBB_DENSE_AUDIT_JSON", "/root/data/mlbb/dense_audit_2026-07-08.json"))
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    out: list[tuple[Path, float, dict, str]] = []
    for block in data.get("vods", []):
        vod_name = str(block.get("vod") or "")
        vid = vod_name.replace("yt_", "").replace(".mp4", "")
        vod = _resolve_vod(inbox, vid)
        if vod is None:
            continue
        for hit in block.get("banner_times") or []:
            sec = float(hit["sec"])
            cid = check_id(vod, sec)
            if cid in labeled or cid in sent:
                continue
            out.append((vod, sec, hit, cid))
    return out


def _owner_good_rows(inbox: Path, labeled: dict, sent: set) -> list[tuple[Path, float, dict, str]]:
    owner = Path(os.environ.get("MLBB_OWNER_LABELS_PATH", "/root/data/mlbb/mobile_legends_owner_labels.json"))
    if not owner.exists():
        return []
    data = json.loads(owner.read_text(encoding="utf-8"))
    out: list[tuple[Path, float, dict, str]] = []
    for vid, marks in (data.get("videos") or {}).items():
        vod = _resolve_vod(inbox, vid)
        if vod is None:
            continue
        for mark in marks:
            if str(mark.get("label")) not in ("good", "yes"):
                continue
            sec = float(mark.get("time_sec") or mark.get("sec") or 0)
            cid = check_id(vod, sec)
            if cid in labeled or cid in sent:
                continue
            out.append((vod, sec, mark, cid))
    return out


def _segment_rows(inbox: Path, labeled: dict, sent: set) -> list[tuple[Path, float, dict, str]]:
    idx_path = Path(os.environ.get("MLBB_VOD_SEGMENT_INDEX", "/root/data/mlbb/vod_segment_index.json"))
    if not idx_path.exists():
        return []
    data = json.loads(idx_path.read_text(encoding="utf-8"))
    out: list[tuple[Path, float, dict, str]] = []
    for row in data.get("segments", []):
        if not row.get("kill_banner"):
            continue
        vod_path = str(row.get("vod") or "")
        vod = Path(vod_path) if vod_path and Path(vod_path).exists() else None
        if vod is None:
            vid = str(row.get("vod_id") or str(row.get("segment_id", "")).rsplit("_", 1)[0])
            vod = _resolve_vod(inbox, vid)
        if vod is None:
            continue
        sec = float(row.get("peak_start") or row.get("start") or 0)
        cid = check_id(vod, sec)
        if cid in labeled or cid in sent:
            continue
        hit = {
            "tier": row.get("kill_banner_tier") or 5,
            "label": row.get("kill_banner") or "savage",
            "text": "segment_index",
            "source": "index",
        }
        out.append((vod, sec, hit, cid))
    return out


def main() -> int:
    env = {**os.environ, **load_env(ENV_PATH)}
    token = env.get("TG_BOT_TOKEN", "")
    chat_id = env.get("TG_CHAT_ID", "")
    if not token or not chat_id:
        print("missing telegram creds")
        return 1

    inbox = Path(os.environ.get("MLBB_VOD_INBOX", "/root/data/mlbb/youtube_nightly/inbox"))
    labeled = labeled_ids()
    sent = load_sent()

    rows: list[tuple[Path, float, dict, str]] = []
    seen: set[str] = set()
    for source in (_audit_rows, _segment_rows, _owner_good_rows):
        for item in source(inbox, labeled, sent):
            cid = item[3]
            if cid in seen:
                continue
            seen.add(cid)
            rows.append(item)
            if len(rows) >= BATCH:
                break
        if len(rows) >= BATCH:
            break

    rows = rows[:BATCH]
    print(json.dumps({"candidates": len(rows), "ids": [r[3] for r in rows]}, ensure_ascii=False))

    sent_n = 0
    for i, (vod, sec, hit, cid) in enumerate(rows, start=1):
        kh = KillBannerHit(
            sec=sec,
            tier=int(hit.get("tier") or 3),
            label=str(hit.get("label") or "banner"),
            text=str(hit.get("text") or hit.get("note") or "")[:80],
            source=str(hit.get("source") or "burst"),
        )
        try:
            shot, meta = render_check_screenshot(vod, sec, hit=kh)
        except Exception as exc:
            print(f"capture_fail {cid}: {exc}")
            continue
        from mlbb_banner_calibration_positive_feed import _read_frame, verified_before_send

        frame = _read_frame(vod, sec)
        ok, why = verified_before_send(vod, kh, frame)
        if not ok:
            print(f"skip_burst {cid}: {why}")
            continue
        st = stats()
        caption = (
            f"🎯 Банер-калибровка {i}/{len(rows)} | размечено {st['labeled']}/{st['target']}\n"
            f"VOD {meta.get('vod_id', '')} @ {sec:.1f}s\n"
            f"бот: {kh.label} tier={kh.tier}\n"
            f"#{cid}\n"
            f"Зелёная рамка = зона банера. Нажми причину."
        )
        if send_photo_file(token, chat_id, shot, caption, reply_markup=inline_keyboard_markup(cid)):
            mark_sent([cid])
            sent_n += 1
            print(f"sent {cid}")

    print(json.dumps({"sent": sent_n, "stats": stats()}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
