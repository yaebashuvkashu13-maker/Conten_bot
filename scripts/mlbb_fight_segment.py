#!/usr/bin/env python3
"""MLBB fight-boundary segmentation — variable clip length from combat sustain."""

from __future__ import annotations

import os
from pathlib import Path

import numpy as np


def _fight_min_sec() -> float:
    return float(os.environ.get("MLBB_FIGHT_MIN_SEC", "7"))


def _fight_max_sec() -> float:
    return float(os.environ.get("MLBB_FIGHT_MAX_SEC", "35"))


def _sustain_quiet_bins() -> int:
    return int(os.environ.get("MLBB_FIGHT_SUSTAIN_QUIET_BINS", "3"))


def _extend_bins(max_d: float, win: float) -> int:
    return int(os.environ.get("MLBB_FIGHT_EXTEND_BINS", str(int(max_d / max(win, 0.5)) + 6)))


def _lead_sec() -> float:
    return float(os.environ.get("MLBB_VOD_LEAD_SEC", "4"))


_CACHE: dict[str, dict] = {}


def _analysis_for(vod: Path) -> dict:
    """One analyze_video pass per VOD file — pool scan calls this dozens of times."""
    mtime = vod.stat().st_mtime_ns
    key = f"{vod.resolve()}:{mtime}"
    if key not in _CACHE:
        from smart_video_editor import analyze_video

        _CACHE[key] = analyze_video(vod)
    return _CACHE[key]


def clear_analysis_cache() -> None:
    _CACHE.clear()


def detect_fight_bounds(vod: Path, peak_sec: float) -> tuple[float, float, float]:
    """
    Detect fight window around peak_sec.

    Returns (start_sec, end_sec, duration_sec), clamped to [7, 22]s.
    Uses sustain decay walk from smart_video_editor.build_candidates logic.
    """
    min_d = _fight_min_sec()
    max_d = _fight_max_sec()
    lead = _lead_sec()
    analysis = _analysis_for(vod)
    win = float(analysis.get("window_seconds", 2.0))
    file_dur = float(analysis.get("duration", 0.0))
    bins = int(analysis.get("bins", 0))
    if bins < 2 or file_dur <= 0:
        start = max(0.0, peak_sec - lead)
        end = min(file_dur, start + min(max_d, 15.0))
        return round(start, 2), round(end, 2), round(end - start, 2)

    motion = np.asarray(analysis["center_motion"], dtype=np.float32)
    audio = np.asarray(analysis["audio"], dtype=np.float32)
    scene = np.asarray(analysis["scene"], dtype=np.float32)
    combined = motion * 0.45 + audio * 0.35 + scene * 0.20

    sustain_thr = float(np.percentile(combined, 42)) if bins > 4 else float(combined.max()) * 0.72
    motion_thr = float(np.percentile(motion, 52)) if bins > 3 else float(motion.max()) * 0.5

    peak_idx = int(round(float(peak_sec) / win))
    peak_idx = max(0, min(bins - 1, peak_idx))

    extend = _extend_bins(max_d, win)
    quiet_need = _sustain_quiet_bins()

    left = peak_idx
    quiet = 0
    while left > 0 and peak_idx - left < extend:
        probe = left - 1
        active = combined[probe] >= sustain_thr or motion[probe] >= motion_thr
        left = probe
        if active:
            quiet = 0
        else:
            quiet += 1
            if quiet >= quiet_need:
                break

    right = peak_idx
    quiet = 0
    while right < bins - 1 and right - peak_idx < extend:
        probe = right + 1
        active = combined[probe] >= sustain_thr * 0.92 or motion[probe] >= motion_thr * 0.95
        right = probe
        if active:
            quiet = 0
        else:
            quiet += 1
            if quiet >= quiet_need:
                break

    region_start = left * win
    region_end = min(file_dur, (right + 1) * win)
    region_dur = max(min_d, region_end - region_start)

    start = max(0.0, min(region_start, float(peak_sec) - lead))
    end = min(file_dur, max(start + region_dur, float(peak_sec) + (region_dur - lead)))
    dur = end - start

    if dur < min_d:
        end = min(file_dur, start + min_d)
        dur = end - start
    if dur > max_d:
        # Keep climax tail — don't cut teamfight mid-resolution.
        end = min(file_dur, region_end)
        start = max(0.0, end - max_d)
        dur = end - start

    return round(start, 2), round(end, 2), round(dur, 2)


def variable_length_enabled() -> bool:
    return os.environ.get("MLBB_VOD_VARIABLE_LENGTH", "1") == "1"
