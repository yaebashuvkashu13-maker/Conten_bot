#!/usr/bin/env python3
"""Post-label hook shared by every MLBB owner-feedback source."""

from __future__ import annotations

import os
import re
import time
from pathlib import Path
from typing import Any

from vod_scan_state import invalidate_pool_cache
from vod_state_io import load_json_state, save_json_state


def _data_root() -> Path:
    return Path(os.environ.get("MLBB_DATA_ROOT", "/root/data/mlbb"))


def _state_path() -> Path:
    return Path(
        os.environ.get(
            "MLBB_VOD_STATE_PATH",
            str(_data_root() / "vod_segment_state.json"),
        )
    )


def _manifest_path() -> Path:
    return Path(
        os.environ.get(
            "MLBB_OWNER_FEEDBACK_MANIFEST",
            str(_data_root() / "owner_feedback_manifest.json"),
        )
    )


def _video_id(value: str) -> str:
    text = str(value or "").strip()
    name = Path(text).stem
    if name.startswith("yt_"):
        name = name[3:]
    match = re.search(r"([A-Za-z0-9_-]{11})", name)
    return match.group(1) if match else name[:24]


def invalidate_vod_feedback(video_id: str, *, state_path: Path | None = None) -> int:
    """Invalidate stale decisions for one labeled VOD and make it scannable again."""
    vid = _video_id(video_id)
    if not vid:
        return 0
    path = state_path or _state_path()
    state = load_json_state(path, {"vods": []})
    changed = 0
    for row in state.get("vods", []):
        if not isinstance(row, dict):
            continue
        row_id = _video_id(str(row.get("id") or row.get("path") or row.get("file") or ""))
        if row_id != vid:
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


def _clear_runtime_caches() -> None:
    try:
        from highlight_scorer import clear_exemplar_cache

        clear_exemplar_cache()
    except Exception:
        pass
    try:
        from mlbb_banner_ref_match import clear_banner_ref_cache

        clear_banner_ref_cache()
    except Exception:
        pass
    try:
        from mlbb_kill_banner import clear_banner_discovery_cache

        clear_banner_discovery_cache()
    except Exception:
        pass
    try:
        from mlbb_feedback_gate_tune import clear_patterns_cache

        clear_patterns_cache()
    except Exception:
        pass
    try:
        from mlbb_fight_segment import clear_analysis_cache

        clear_analysis_cache()
    except Exception:
        pass


def record_owner_feedback(
    *,
    source: str,
    video_id: str,
    time_sec: float,
    label: str,
    reason: str = "",
    item_id: str = "",
) -> dict[str, Any]:
    """Record one feedback event, invalidate decisions, and bump dataset version."""
    if label not in ("good", "bad"):
        raise ValueError(f"unsupported owner label: {label}")
    vid = _video_id(video_id)
    invalidated = invalidate_vod_feedback(vid)
    path = _manifest_path()
    manifest = load_json_state(
        path,
        {
            "version": 0,
            "counts": {},
            "events": [],
        },
    )
    version = int(manifest.get("version") or 0) + 1
    counts = manifest.setdefault("counts", {})
    count_key = f"{source}:{label}"
    counts[count_key] = int(counts.get(count_key) or 0) + 1
    events = list(manifest.get("events") or [])
    events.append(
        {
            "version": version,
            "source": source,
            "video_id": vid,
            "time_sec": round(float(time_sec), 1),
            "label": label,
            "reason": str(reason or "")[:200],
            "item_id": str(item_id or "")[:160],
            "at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
    )
    manifest.update(
        {
            "version": version,
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "counts": counts,
            "events": events[-2000:],
        }
    )
    save_json_state(path, manifest)
    _clear_runtime_caches()
    # Do not pause all sends on every owner tap while learning — only on bad when gate is strict.
    if (
        label == "bad"
        and os.environ.get("MLBB_LEARNING_FIRST", "0") == "1"
        and os.environ.get("MLBB_SEND_ENABLED", "1") != "1"
    ):
        try:
            from mlbb_learning_first import set_transition_passed

            set_transition_passed(False)
        except Exception:
            pass
    return {
        "version": version,
        "invalidated_vods": invalidated,
        "video_id": vid,
    }
