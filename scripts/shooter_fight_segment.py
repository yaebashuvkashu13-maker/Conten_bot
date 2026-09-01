#!/usr/bin/env python3
"""PUBG/Standoff fight-boundary segmentation — variable montage part length."""

from __future__ import annotations

import os
from pathlib import Path

import numpy as np


def _fight_min_sec() -> float:
    return float(os.environ.get("SHOOTER_FIGHT_MIN_SEC", "12"))


def _fight_max_sec() -> float:
    return float(os.environ.get("SHOOTER_FIGHT_MAX_SEC", "28"))


def _fight_hard_max_sec() -> float:
    return float(os.environ.get("SHOOTER_FIGHT_HARD_MAX_SEC", "36"))


def _lead_sec() -> float:
    return float(os.environ.get("SHOOTER_VOD_LEAD_SEC", "3"))


def _sustain_quiet_bins() -> int:
    return int(os.environ.get("SHOOTER_FIGHT_SUSTAIN_QUIET_BINS", "2"))


def variable_length_enabled() -> bool:
    return os.environ.get("SHOOTER_VOD_VARIABLE_LENGTH", "1") == "1"


def _analysis_for(vod: Path) -> dict:
    from vod_analysis_cache import analyze_video_cached

    return analyze_video_cached(vod)


def detect_shooter_fight_bounds(vod: Path, peak_sec: float) -> tuple[float, float, float]:
    """
    Gunfire-sustain window around peak_sec.

    Returns (start_sec, end_sec, duration_sec). Uses cached analyze_video bins
    (gunfire + center_motion) — one pass per VOD, no per-peak ffmpeg.
    """
    min_d = _fight_min_sec()
    max_d = _fight_max_sec()
    lead = _lead_sec()
    analysis = _analysis_for(vod)
    win = float(analysis.get("window_seconds", 2.0))
    file_dur = float(analysis.get("duration", 0.0))
    bins = int(analysis.get("bins", 0))
    if bins < 2 or file_dur <= 0:
        half = min(max_d, max(min_d, 14.0)) * 0.5
        start = max(0.0, float(peak_sec) - half)
        end = min(file_dur, start + max(min_d, half * 2))
        return round(start, 2), round(end, 2), round(end - start, 2)

    gunfire = np.asarray(analysis.get("gunfire", analysis.get("audio", [])), dtype=np.float32)
    motion = np.asarray(analysis.get("center_motion", analysis.get("motion", [])), dtype=np.float32)
    if gunfire.size < bins:
        gunfire = np.resize(gunfire, bins)
    if motion.size < bins:
        motion = np.resize(motion, bins)

    combined = gunfire * 0.62 + motion * 0.38
    gun_thr = float(np.percentile(gunfire, 55)) if bins > 4 else float(gunfire.max()) * 0.65
    sustain_thr = float(np.percentile(combined, 40)) if bins > 4 else float(combined.max()) * 0.70
    motion_thr = float(np.percentile(motion, 48)) if bins > 3 else float(motion.max()) * 0.55

    peak_idx = int(round(float(peak_sec) / win))
    peak_idx = max(0, min(bins - 1, peak_idx))

    extend = int(os.environ.get("SHOOTER_FIGHT_EXTEND_BINS", str(int(max_d / max(win, 0.5)) + 4)))
    quiet_need = _sustain_quiet_bins()

    left = peak_idx
    quiet = 0
    while left > 0 and peak_idx - left < extend:
        probe = left - 1
        active = (
            gunfire[probe] >= gun_thr * 0.85
            or combined[probe] >= sustain_thr
            or motion[probe] >= motion_thr
        )
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
        active = (
            gunfire[probe] >= gun_thr * 0.80
            or combined[probe] >= sustain_thr * 0.92
            or motion[probe] >= motion_thr * 0.95
        )
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

    hard_max = min(_fight_hard_max_sec(), max_d * 1.25)
    if dur > max_d:
        # Keep peak inside the window; prefer extending past the fight over pre-roll.
        peak = float(peak_sec)
        tail = max(lead, (end - peak))
        head = max(lead, (peak - start))
        if tail >= head:
            start = max(0.0, peak - min(max_d * 0.35, head))
            end = min(file_dur, max(start + max_d, peak + (max_d - (peak - start))))
        else:
            end = min(file_dur, peak + min(max_d * 0.65, tail))
            start = max(0.0, end - max_d)
        dur = end - start

    if dur > hard_max:
        end = min(file_dur, region_end)
        start = max(0.0, end - hard_max)
        dur = end - start

    return round(start, 2), round(end, 2), round(dur, 2)
