#!/usr/bin/env python3
"""Learn which MLBB uploaders and search queries produce owner-approved clips."""

from __future__ import annotations

import os
import time
import argparse
import json
from pathlib import Path

from vod_state_io import load_json_state, save_json_state
from youtube_mlbb_vod_prefs import normalize_uploader


def _path() -> Path:
    root = Path(os.environ.get("MLBB_DATA_ROOT", "/root/data/mlbb"))
    return Path(os.environ.get("MLBB_SOURCE_YIELD_PATH", str(root / "source_yield.json")))


def load_source_yield() -> dict:
    data = load_json_state(
        _path(),
        {"schema_version": 1, "uploaders": {}, "queries": {}, "videos": {}},
    )
    for key in ("uploaders", "queries", "videos"):
        data.setdefault(key, {})
    return data


def _query_key(meta: dict) -> str:
    return " ".join(str(meta.get("search_query") or "").strip().casefold().split())[:240]


def _stats(data: dict, kind: str, key: str) -> dict:
    rows = data.setdefault(kind, {})
    return rows.setdefault(
        key,
        {"vods": 0, "clips_sent": 0, "good": 0, "bad": 0, "last_at": ""},
    )


def _touch(row: dict) -> None:
    row["last_at"] = time.strftime("%Y-%m-%d %H:%M:%S")


def record_vod_outcome(meta: dict | None, *, sent: int, reject_reason: str = "") -> None:
    if not meta:
        return
    vid = str(meta.get("id") or "").strip()
    if not vid:
        return
    data = load_source_yield()
    videos = data["videos"]
    previous = videos.get(vid)
    uploader = normalize_uploader(meta)
    query = _query_key(meta)
    if previous is None:
        for kind, key in (("uploaders", uploader), ("queries", query)):
            if not key:
                continue
            row = _stats(data, kind, key)
            row["vods"] = int(row.get("vods") or 0) + 1
            row["clips_sent"] = int(row.get("clips_sent") or 0) + max(0, int(sent))
            _touch(row)
    elif int(sent) > int(previous.get("sent") or 0):
        delta = int(sent) - int(previous.get("sent") or 0)
        for kind, key in (("uploaders", uploader), ("queries", query)):
            if key:
                row = _stats(data, kind, key)
                row["clips_sent"] = int(row.get("clips_sent") or 0) + delta
                _touch(row)
    videos[vid] = {
        "uploader": uploader,
        "query": query,
        "sent": max(int(sent), int((previous or {}).get("sent") or 0)),
        "reject_reason": str(reject_reason or "")[:160],
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "labels": dict((previous or {}).get("labels") or {}),
    }
    data["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    save_json_state(_path(), data)


def record_owner_feedback(
    video_id: str,
    *,
    label: str,
    item_id: str = "",
) -> None:
    if label not in ("good", "bad"):
        return
    data = load_source_yield()
    video = data.get("videos", {}).get(str(video_id))
    if not isinstance(video, dict):
        return
    labels = video.setdefault("labels", {})
    key = str(item_id or f"{video_id}:{label}")
    previous = labels.get(key)
    if previous == label:
        return
    uploader = str(video.get("uploader") or "")
    query = str(video.get("query") or "")
    if previous in ("good", "bad"):
        for kind, source_key in (("uploaders", uploader), ("queries", query)):
            if source_key:
                row = _stats(data, kind, source_key)
                row[previous] = max(0, int(row.get(previous) or 0) - 1)
    for kind, source_key in (("uploaders", uploader), ("queries", query)):
        if source_key:
            row = _stats(data, kind, source_key)
            row[label] = int(row.get(label) or 0) + 1
            _touch(row)
    labels[key] = label
    data["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    save_json_state(_path(), data)


def _row_adjustment(row: dict) -> float:
    attempts = int(row.get("vods") or 0)
    if attempts <= 0:
        return 1.5
    sent = int(row.get("clips_sent") or 0)
    good = int(row.get("good") or 0)
    bad = int(row.get("bad") or 0)
    reliability = min(1.0, attempts / 5.0)
    yield_rate = (sent + 1.0) / (attempts + 3.0)
    precision = (good + 2.0) / (good + bad + 4.0)
    exploration = max(0.0, 1.5 - attempts * 0.25)
    return reliability * ((yield_rate - 0.25) * 8.0 + (precision - 0.5) * 10.0) + exploration


def source_rank_adjustment(meta: dict) -> float:
    data = load_source_yield()
    uploader = normalize_uploader(meta)
    query = _query_key(meta)
    score = 0.0
    if uploader:
        score += _row_adjustment(data.get("uploaders", {}).get(uploader, {}))
    if query:
        score += 0.6 * _row_adjustment(data.get("queries", {}).get(query, {}))
    return score


def video_rank_adjustment(video_id: str) -> float:
    video = load_source_yield().get("videos", {}).get(str(video_id), {})
    labels = list((video.get("labels") or {}).values()) if isinstance(video, dict) else []
    good = sum(label == "good" for label in labels)
    bad = sum(label == "bad" for label in labels)
    if good + bad == 0:
        return 0.0
    return 4.0 * (good - bad) / (good + bad)


def uploader_hard_blocked(meta: dict) -> bool:
    """Require repeated failure plus negative feedback; never block after one VOD."""
    uploader = normalize_uploader(meta)
    if not uploader:
        return False
    row = load_source_yield().get("uploaders", {}).get(uploader, {})
    attempts = int(row.get("vods") or 0)
    sent = int(row.get("clips_sent") or 0)
    good = int(row.get("good") or 0)
    bad = int(row.get("bad") or 0)
    min_attempts = max(2, int(os.environ.get("MLBB_SOURCE_BLOCK_MIN_VODS", "3")))
    return attempts >= min_attempts and sent == 0 and bad >= 2 and good == 0


def bootstrap_existing() -> dict:
    """One-shot import of registry outcomes and historical VOD feedback."""
    root = Path(os.environ.get("MLBB_DATA_ROOT", "/root/data/mlbb"))
    try:
        state = json.loads((root / "vod_segment_state.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        state = {"vods": []}
    try:
        labels = json.loads((root / "vod_segment_labels.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        labels = {"feedback": []}
    data = load_source_yield()
    added_vods = added_labels = 0
    for meta in state.get("vods", []):
        vid = str(meta.get("id") or "").strip()
        if not vid or vid in data["videos"] or not meta.get("last_scan_at"):
            continue
        uploader = normalize_uploader(meta)
        query = _query_key(meta)
        sent = max(0, int(meta.get("last_scan_sent") or 0))
        for kind, key in (("uploaders", uploader), ("queries", query)):
            if key:
                row = _stats(data, kind, key)
                row["vods"] = int(row.get("vods") or 0) + 1
                row["clips_sent"] = int(row.get("clips_sent") or 0) + sent
                _touch(row)
        data["videos"][vid] = {
            "uploader": uploader,
            "query": query,
            "sent": sent,
            "reject_reason": str(meta.get("reject_reason") or "")[:160],
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "labels": {},
        }
        added_vods += 1
    for item in labels.get("feedback", []):
        sid = str(item.get("segment_id") or "")
        vid = str(item.get("vod_id") or (sid.rsplit("_", 1)[0] if "_" in sid else ""))
        video = data["videos"].get(vid)
        label = "good" if item.get("owner_label") in ("yes", "good") else "bad"
        if not video or not sid or sid in video.setdefault("labels", {}):
            continue
        for kind, key in (
            ("uploaders", str(video.get("uploader") or "")),
            ("queries", str(video.get("query") or "")),
        ):
            if key:
                row = _stats(data, kind, key)
                row[label] = int(row.get(label) or 0) + 1
                _touch(row)
        video["labels"][sid] = label
        added_labels += 1
    data["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    save_json_state(_path(), data)
    return {"added_vods": added_vods, "added_labels": added_labels}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bootstrap", action="store_true")
    args = parser.parse_args()
    if args.bootstrap:
        print(json.dumps(bootstrap_existing(), ensure_ascii=False))
        return 0
    parser.error("--bootstrap is required")


if __name__ == "__main__":
    raise SystemExit(main())
