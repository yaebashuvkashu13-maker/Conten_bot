#!/usr/bin/env python3
"""Sync PUBG Shorts 👍/👎 into pubg_owner_labels.json for highlight training."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

REPO = Path(os.environ.get("CONTENT_BOT_REPO", "/root/content_bot_ml"))
DATA_PUBG = Path(os.environ.get("SHOOTER_PUBG_DATA_ROOT", "/root/data/pubg"))
OWNER_PATH = Path(
    os.environ.get("PUBG_OWNER_LABELS_PATH", str(REPO / "data" / "pubg_owner_labels.json"))
)


def _owner_path() -> Path:
    return Path(
        os.environ.get("PUBG_OWNER_LABELS_PATH", str(REPO / "data" / "pubg_owner_labels.json"))
    )
SHORTS_LABELS = Path(
    os.environ.get("PUBG_CALIBRATION_LABELS", str(DATA_PUBG / "calibration_labels.json"))
)

SHORTS_SCOPE = "youtube_shorts"
VOD_SCOPE = "vod_segment"


def _video_id_from_segment(segment_id: str, vod: str) -> str:
    vid = str(vod or "").strip()
    if vid.startswith("yt_"):
        vid = vid[3:]
    if vid.endswith(".mp4"):
        vid = Path(vid).stem
        if vid.startswith("yt_"):
            vid = vid[3:]
    if not vid and "_" in segment_id:
        vid = segment_id.rsplit("_", 1)[0]
    return vid


def sync_vod_segment_to_owner_json(
    video_id: str,
    time_sec: float,
    *,
    is_good: bool,
    reason: str = "",
    segment_id: str = "",
) -> bool:
    note = reason or (f"vseg_{segment_id}" if segment_id else "vod_segment")
    return append_owner_time_label(
        video_id,
        time_sec,
        "good" if is_good else "bad",
        note=note,
        source=VOD_SCOPE,
    )


def _read_json(path: Path, default: dict | list) -> dict | list:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return default


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def load_owner_labels_json() -> dict:
    data = _read_json(_owner_path(), {"videos": {}})
    if not isinstance(data, dict):
        return {"videos": {}}
    data.setdefault("videos", {})
    return data


def save_owner_labels_json(data: dict) -> None:
    data["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    path = _owner_path()
    _write_json(path, data)
    legacy = Path("/root/data/mlbb/pubg_owner_labels.json")
    if legacy.parent.exists() and str(path.resolve()) != str(legacy.resolve()):
        try:
            _write_json(legacy, data)
        except OSError:
            pass


def append_owner_time_label(
    video_id: str,
    time_sec: float,
    label: str,
    *,
    note: str = "",
    source: str = "owner",
) -> bool:
    vid = video_id.strip()
    if not vid or label not in ("good", "bad"):
        return False
    data = load_owner_labels_json()
    rows: list[dict] = list(data.setdefault("videos", {}).get(vid, []))
    key = (round(float(time_sec), 1), label, source)
    seen = {
        (round(float(r.get("time_sec", 0)), 1), r.get("label"), r.get("source", ""))
        for r in rows
        if "time_sec" in r
    }
    if key in seen:
        return False
    entry: dict = {"time_sec": round(float(time_sec), 1), "label": label, "source": source}
    if note:
        entry["note"] = note[:200]
    rows.append(entry)
    data["videos"][vid] = rows
    save_owner_labels_json(data)
    return True


def sync_shorts_label_to_owner_json(video_id: str, *, is_good: bool, reason: str = "") -> bool:
    note = reason or ("shorts_good" if is_good else "shorts_bad")
    return append_owner_time_label(
        video_id,
        0.0,
        "good" if is_good else "bad",
        note=note,
        source=SHORTS_SCOPE,
    )


def backfill_shorts_to_owner_labels() -> int:
    data = _read_json(SHORTS_LABELS, {"good": [], "bad": [], "feedback": []})
    if not isinstance(data, dict):
        return 0
    added = 0
    for bucket, label in (("good", "good"), ("bad", "bad")):
        for row in data.get(bucket, []):
            vid = str(row.get("video_id") or row.get("id") or "").strip()
            if not vid:
                continue
            if sync_shorts_label_to_owner_json(
                vid,
                is_good=label == "good",
                reason=str(row.get("reason") or ""),
            ):
                added += 1
    return added
