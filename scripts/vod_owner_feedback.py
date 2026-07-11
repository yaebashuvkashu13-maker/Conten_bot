#!/usr/bin/env python3
"""Cross-game VOD feedback hook: invalidate stale decisions and version labels."""

from __future__ import annotations

import os
import time
from pathlib import Path

from shooter_vod_segment_store import _paths, vod_youtube_id
from vod_scan_state import invalidate_pool_cache
from vod_state_io import load_json_state, save_json_state


def invalidate_vod_feedback(game: str, video_id: str) -> int:
    game = game.strip().lower()
    vid = vod_youtube_id(Path(str(video_id)))
    path = _paths(game)["state"]
    state = load_json_state(path, {"vods": []})
    changed = 0
    for row in state.get("vods", []):
        if not isinstance(row, dict):
            continue
        row_vid = vod_youtube_id(Path(str(row.get("path") or row.get("id") or "")))
        if row_vid != vid:
            continue
        invalidate_pool_cache(row)
        for key in (
            "last_scan_at",
            "last_scan_blocked",
            "last_scan_sent",
            "reject_reason",
            "zero_send_sessions",
        ):
            row.pop(key, None)
        row["exhausted"] = False
        row["feedback_epoch"] = time.time()
        changed += 1
    if changed:
        state["feedback_epoch"] = time.time()
        save_json_state(path, state)
    return changed


def record_owner_feedback(
    game: str,
    *,
    video_id: str,
    time_sec: float,
    label: str,
    reason: str = "",
    item_id: str = "",
) -> dict:
    game = game.strip().lower()
    if label not in ("good", "bad"):
        raise ValueError(f"unsupported label: {label}")
    invalidated = invalidate_vod_feedback(game, video_id)
    root = _paths(game)["state"].parent
    manifest_path = root / "owner_feedback_manifest.json"
    data = load_json_state(manifest_path, {"version": 0, "counts": {}, "events": []})
    version = int(data.get("version") or 0) + 1
    counts = data.setdefault("counts", {})
    key = f"vod_segment:{label}"
    counts[key] = int(counts.get(key) or 0) + 1
    events = list(data.get("events") or [])
    events.append(
        {
            "version": version,
            "game": game,
            "video_id": vod_youtube_id(Path(str(video_id))),
            "time_sec": round(float(time_sec), 1),
            "label": label,
            "reason": str(reason or "")[:200],
            "item_id": str(item_id or "")[:160],
            "at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
    )
    data.update(
        {
            "version": version,
            "counts": counts,
            "events": events[-2000:],
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
    )
    save_json_state(manifest_path, data)
    try:
        from highlight_scorer import clear_exemplar_cache

        clear_exemplar_cache()
    except Exception:
        pass
    try:
        from vod_quality_model import clear_model_cache

        clear_model_cache(game)
    except Exception:
        pass
    return {"version": version, "invalidated_vods": invalidated}
