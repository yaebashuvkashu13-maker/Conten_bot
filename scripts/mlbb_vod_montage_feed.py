#!/usr/bin/env python3
"""MLBB vertical montage 33-57s from owner-good VOD fights."""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from montage_env import strict_peak_env
from strict_montage_direct import file_sha256, make_strict_montage
from youtube_download import load_env

ENV_PATH = Path("/root/.video_bot.env")
PROFILE = "mobile_legends"
LABELS_PATH = Path(os.environ.get("MLBB_VOD_SEGMENT_LABELS", "/root/data/mlbb/vod_segment_labels.json"))
INBOX = Path(os.environ.get("HIGHLIGHT_INBOX", "/root/data/mlbb/youtube_nightly/inbox"))
STATE_PATH = Path(os.environ.get("MLBB_MONTAGE_STATE", "/root/data/mlbb/mlbb_montage_state.json"))
log = logging.getLogger("mlbb_montage_feed")


def _parse_at(value: str) -> datetime | None:
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(value.strip(), fmt)
        except ValueError:
            continue
    return None


def good_counts_by_vod() -> dict[str, int]:
    if not LABELS_PATH.exists():
        return {}
    data = json.loads(LABELS_PATH.read_text(encoding="utf-8"))
    counts: dict[str, int] = {}
    for row in data.get("good", []):
        sid = str(row.get("segment_id", ""))
        vod_path = str(row.get("vod", ""))
        if vod_path:
            from mlbb_vod_segment_store import vod_youtube_id

            vid = vod_youtube_id(Path(vod_path))
        elif "_" in sid:
            vid = sid.rsplit("_", 1)[0]
        else:
            continue
        counts[vid] = counts.get(vid, 0) + 1
    return counts


def good_last_24h() -> int:
    if not LABELS_PATH.exists():
        return 0
    data = json.loads(LABELS_PATH.read_text(encoding="utf-8"))
    cutoff = datetime.now() - timedelta(hours=24)
    n = 0
    for row in data.get("good", []):
        at = _parse_at(str(row.get("at", "")))
        if at and at >= cutoff:
            n += 1
    return n


def resolve_vod(vid: str) -> Path | None:
    path = INBOX / f"yt_{vid}.mp4"
    return path if path.exists() else None


def load_state() -> dict:
    if not STATE_PATH.exists():
        return {"sent_vods": [], "last_sent_at": ""}
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"sent_vods": [], "last_sent_at": ""}


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


def pick_vod() -> tuple[str, Path] | None:
    counts = good_counts_by_vod()
    state = load_state()
    sent = set(state.get("sent_vods", []))
    min_on_vod = int(os.environ.get("MLBB_MONTAGE_MIN_GOOD_ON_VOD", "3"))
    min_24h = int(os.environ.get("MLBB_MONTAGE_MIN_GOOD_24H", "6"))

    if good_last_24h() >= min_24h:
        for vid, _n in sorted(counts.items(), key=lambda x: x[1], reverse=True):
            if vid in sent:
                continue
            vod = resolve_vod(vid)
            if vod:
                return vid, vod

    for vid, n in sorted(counts.items(), key=lambda x: x[1], reverse=True):
        if n < min_on_vod or vid in sent:
            continue
        vod = resolve_vod(vid)
        if vod:
            return vid, vod
    return None


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if os.environ.get("MLBB_ONLY_MODE", "1") != "1":
        print("SKIP: MLBB_ONLY_MODE not set")
        return 0

    env = {**os.environ, **load_env(ENV_PATH)}
    for key, val in strict_peak_env(PROFILE).items():
        os.environ[key] = val
        env[key] = val

    picked = pick_vod()
    if not picked:
        log.info("montage skip: need >=3 good on VOD or >=6 good/24h")
        return 0

    vid, vod = picked
    basename = f"mlbb_montage_{vid}_{int(time.time())}"
    caption = (
        f"MLBB монтаж 33-57с\n"
        f"VOD {vid} | 3-4 боя | vertical 720x1280\n"
        f"👍/👎 под видео"
    )
    rc, detail = make_strict_montage(
        profile=PROFILE,
        vod=vod,
        output_basename=basename,
        caption=caption,
        env=env,
    )
    if rc == 0:
        state = load_state()
        sent = list(state.get("sent_vods", []))
        sent.append(vid)
        state["sent_vods"] = sent[-40:]
        state["last_sent_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        state["last_detail"] = detail
        save_state(state)
        log.info("montage OK %s", detail)
        return 0
    log.warning("montage FAIL rc=%s %s", rc, detail)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
