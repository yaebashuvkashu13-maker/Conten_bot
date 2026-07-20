#!/usr/bin/env python3
"""WoT brawl segment validation — hit flashes + impact density, reject cruise."""

from __future__ import annotations

import os
from pathlib import Path

from visual_action_check import check_frame_visual, segment_frame_times


def _min_hit_flashes() -> int:
    return max(1, int(os.environ.get("WOT_BRAWL_MIN_HIT_FLASHES", "2")))


def _min_impact_density() -> float:
    return float(os.environ.get("WOT_BRAWL_MIN_IMPACT_DENSITY", "0.052"))


def count_hit_flashes(
    video_path: Path,
    start_sec: float,
    duration_sec: float,
    profile: str = "wot",
) -> tuple[int, float]:
    flashes = 0
    best = 0.0
    from gameplay_gate import _read_frame_at, detect_game_viewport_crop

    crop = detect_game_viewport_crop(video_path, start_sec, duration_sec)
    for _label, t in segment_frame_times(start_sec, duration_sec):
        frame = _read_frame_at(video_path, t)
        if frame is None:
            continue
        if crop is not None:
            x, y, w, h = crop
            frame = frame[y : y + h, x : x + w]
        _ok, _reason, metrics = check_frame_visual(profile, frame)
        flash = float(metrics.get("hit_flash", 0))
        best = max(best, flash)
        if flash >= float(os.environ.get("WOT_BRAWL_FLASH_MIN", "0.004")):
            flashes += 1
    return flashes, best


def _cruise_impact_max() -> float:
    return float(os.environ.get("WOT_BRAWL_CRUISE_IMPACT_MAX", "0.05"))


def validate_wot_brawl_segment(
    video_path: Path,
    start_sec: float,
    duration_sec: float,
    *,
    metrics: dict | None = None,
) -> tuple[bool, str, dict]:
    from strict_segment_gate import probe_segment

    base = dict(metrics or {})
    if not base:
        base = probe_segment(video_path, start_sec, duration_sec, "wot")
    impact = float(base.get("impact_density", 0))
    motion = float(base.get("center_motion", 0))
    flashes, best_flash = count_hit_flashes(video_path, start_sec, duration_sec)
    out = {
        **base,
        "hit_flash_count": flashes,
        "best_hit_flash": round(best_flash, 4),
        "impact_density": impact,
        "center_motion": motion,
    }
    min_impact = _min_impact_density()
    min_flashes = _min_hit_flashes()
    if flashes < min_flashes:
        return False, f"wot_low_flashes={flashes}:need{min_flashes}", out
    if impact < min_impact:
        return False, f"wot_low_impact=density{impact:.3f}:need{min_impact:.3f}", out
    if motion > 0.10 and impact < _cruise_impact_max() and duration_sec > 5.0:
        return False, f"wot_cruise=motion{motion:.3f}:impact{impact:.3f}", out
    return True, f"wot_brawl_ok=flashes{flashes}:impact{impact:.3f}", out
