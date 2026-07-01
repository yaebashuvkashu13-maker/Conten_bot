#!/usr/bin/env python3
"""
Sync VIRAL_MLBB_* thresholds from YouTube Shorts + owner 👍 labels.

Top Shorts by views/day teach hook floor; owner good clips raise CLIP bar.
Writes /root/data/mlbb/viral_thresholds.json and patches .video_bot.env.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mlbb_calibration_store import DATA_MLBB, labeled_ids, load_index, load_labels
from mlbb_vod_segment_store import load_labels as load_vseg_labels

from datetime import datetime, timezone

THRESHOLD_PATH = Path(os.environ.get("MLBB_VIRAL_THRESHOLDS", str(DATA_MLBB / "viral_thresholds.json")))
ENV_PATH = Path(os.environ.get("VIDEO_BOT_ENV", "/root/.video_bot.env"))


def _median(vals: list[float]) -> float:
    if not vals:
        return 0.0
    s = sorted(vals)
    mid = len(s) // 2
    return s[mid] if len(s) % 2 else (s[mid - 1] + s[mid]) / 2.0


def _views_per_day(row: dict) -> float:
    views = int(row.get("view_count") or 0)
    upload = str(row.get("upload_date") or "")
    if len(upload) == 8 and upload.isdigit():
        try:
            uploaded = datetime.strptime(upload, "%Y%m%d").replace(tzinfo=timezone.utc)
            age = max(1.0, (datetime.now(timezone.utc) - uploaded).total_seconds() / 86400.0)
            return views / age
        except ValueError:
            pass
    return float(views)


def compute_thresholds() -> dict:
    rows = list(load_index().get("candidates", []))
    labeled = labeled_ids()
    for row in rows:
        row["_vpd"] = _views_per_day(row)

    top = sorted(rows, key=lambda r: float(r.get("_vpd") or 0), reverse=True)[:50]
    top_hooks = [float(r.get("hook_score") or 0) for r in top if float(r.get("hook_score") or 0) > 0]

    owner_good_hooks: list[float] = []
    for row in load_labels().get("good", []):
        h = float(row.get("hook_score") or 0)
        if h > 0:
            owner_good_hooks.append(h)
    for row in load_vseg_labels().get("good", []):
        h = float(row.get("hook_score") or 0)
        if h > 0:
            owner_good_hooks.append(h)

    owner_bad_hooks = [
        float(r.get("hook_score") or 0)
        for r in load_vseg_labels().get("bad", [])
        if float(r.get("hook_score") or 0) > 0
    ]

    silver_hook = _median(top_hooks)
    owner_hook = _median(owner_good_hooks)
    bad_hook = _median(owner_bad_hooks)

    # Floor from silver Shorts; owner labels nudge up only when we have silver signal.
    hook_floor = float(os.environ.get("VIRAL_MLBB_HOOK_FLOOR", "0.06"))
    hook_cap = float(os.environ.get("VIRAL_MLBB_HOOK_CAP", "0.12"))
    hook_min = max(hook_floor, silver_hook * 0.85)
    if silver_hook > 0 and owner_hook > 0:
        hook_min = max(hook_min, owner_hook * 0.65)
    if silver_hook > 0 and bad_hook > 0:
        hook_min = max(hook_min, bad_hook * 0.55)
    hook_min = min(hook_min, hook_cap)

    clip_min = max(0.08, float(os.environ.get("VIRAL_MLBB_CLIP_HOOK_MIN", "0.12")))
    if silver_hook > 0 and owner_hook > 0:
        clip_min = max(clip_min, hook_min * 0.9)
    clip_cap = float(os.environ.get("VIRAL_MLBB_CLIP_HOOK_CAP", "0.15"))
    clip_min = min(clip_min, clip_cap)

    return {
        "shorts_analyzed": len(rows),
        "top50_count": len(top),
        "silver_hook_median": round(silver_hook, 4),
        "owner_good_hook_median": round(owner_hook, 4),
        "owner_bad_hook_median": round(bad_hook, 4),
        "VIRAL_MLBB_HOOK_MIN": round(hook_min, 4),
        "VIRAL_MLBB_CLIP_HOOK_MIN": round(clip_min, 4),
        "VIRAL_SEGMENT_HOOK_MIN": round(hook_min, 4),
    }


def patch_env(thresholds: dict) -> None:
    if not ENV_PATH.exists():
        return
    text = ENV_PATH.read_text(encoding="utf-8")
    for key in ("VIRAL_MLBB_HOOK_MIN", "VIRAL_MLBB_CLIP_HOOK_MIN", "VIRAL_SEGMENT_HOOK_MIN"):
        val = str(thresholds.get(key, ""))
        if not val:
            continue
        pattern = rf"^{re.escape(key)}=.*$"
        if re.search(pattern, text, flags=re.M):
            text = re.sub(pattern, f"{key}={val}", text, flags=re.M)
        else:
            text = text.rstrip() + f"\n{key}={val}\n"
    ENV_PATH.write_text(text, encoding="utf-8")


def main() -> int:
    thresholds = compute_thresholds()
    THRESHOLD_PATH.parent.mkdir(parents=True, exist_ok=True)
    THRESHOLD_PATH.write_text(json.dumps(thresholds, indent=2, ensure_ascii=False), encoding="utf-8")
    patch_env(thresholds)
    print(
        f"hook_min={thresholds['VIRAL_MLBB_HOOK_MIN']} "
        f"(silver={thresholds['silver_hook_median']} owner={thresholds['owner_good_hook_median']}) "
        f"shorts={thresholds['shorts_analyzed']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
