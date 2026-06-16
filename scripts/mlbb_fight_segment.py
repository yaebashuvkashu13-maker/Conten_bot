#!/usr/bin/env python3
"""MLBB fight-boundary segmentation — variable clip length from combat sustain."""

from __future__ import annotations

import os
from pathlib import Path

import numpy as np


def _fight_min_sec() -> float:
    return float(os.environ.get("MLBB_FIGHT_MIN_SEC", "7"))


def _fight_max_sec() -> float:
    return float(os.environ.get("MLBB_FIGHT_MAX_SEC", "22"))


def _lead_sec() -> float:
    return float(os.environ.get("MLBB_VOD_LEAD_SEC", "4"))


def _trim_head_sec() -> float:
    return float(os.environ.get("MLBB_VOD_TRIM_HEAD_SEC", "0"))


def fight_until_end_enabled() -> bool:
    return os.environ.get("MLBB_FIGHT_UNTIL_END", "0") == "1"


def _max_right_bins() -> int:
    if fight_until_end_enabled():
        return int(os.environ.get("MLBB_FIGHT_MAX_RIGHT_BINS", "45"))
    return int(os.environ.get("MLBB_FIGHT_RIGHT_BINS", "14"))


def _quiet_bins_to_end() -> int:
    return int(os.environ.get("MLBB_FIGHT_QUIET_BINS", "3" if fight_until_end_enabled() else "2"))


def apply_head_trim(start: float, dur: float, file_dur: float) -> tuple[float, float]:
    """Drop dead time at clip start (owner calibration, default 0)."""
    trim = _trim_head_sec()
    min_d = _fight_min_sec()
    if trim <= 0 or dur <= min_d:
        return round(start, 2), round(dur, 2)
    trim = min(trim, max(0.0, dur - min_d))
    start = start + trim
    dur = dur - trim
    if file_dur > 0:
        start = min(start, max(0.0, file_dur - min_d))
        dur = min(dur, max(min_d, file_dur - start))
    return round(start, 2), round(dur, 2)


def detect_fight_bounds(vod: Path, peak_sec: float) -> tuple[float, float, float]:
    """
    Detect fight window around peak_sec.

    Returns (start_sec, end_sec, duration_sec).
    With MLBB_FIGHT_UNTIL_END=1: start at peak-lead (default 4s), end when combat fades.
    """
    from smart_video_editor import analyze_video

    min_d = _fight_min_sec()
    max_d = _fight_max_sec()
    lead = _lead_sec()
    until_end = fight_until_end_enabled()

    analysis = analyze_video(vod)
    win = float(analysis.get("window_seconds", 2.0))
    file_dur = float(analysis.get("duration", 0.0))
    bins = int(analysis.get("bins", 0))
    if bins < 2 or file_dur <= 0:
        start = max(0.0, float(peak_sec) - lead)
        end = min(file_dur, start + min(max_d if max_d > 0 else 15.0, 90.0 if until_end else 15.0))
        start, dur = apply_head_trim(start, end - start, file_dur)
        return start, round(start + dur, 2), dur

    motion = np.asarray(analysis["center_motion"], dtype=np.float32)
    audio = np.asarray(analysis["audio"], dtype=np.float32)
    scene = np.asarray(analysis["scene"], dtype=np.float32)
    combined = motion * 0.45 + audio * 0.35 + scene * 0.20

    sustain_thr = float(np.percentile(combined, 42)) if bins > 4 else float(combined.max()) * 0.72
    motion_thr = float(np.percentile(motion, 52)) if bins > 3 else float(motion.max()) * 0.5

    peak_idx = int(round(float(peak_sec) / win))
    peak_idx = max(0, min(bins - 1, peak_idx))

    left = peak_idx
    quiet = 0
    max_left = 18 if until_end else 12
    while left > 0 and peak_idx - left < max_left:
        probe = left - 1
        active = combined[probe] >= sustain_thr or motion[probe] >= motion_thr
        left = probe
        if active:
            quiet = 0
        else:
            quiet += 1
            if quiet >= _quiet_bins_to_end():
                break

    right = peak_idx
    quiet = 0
    max_right = _max_right_bins()
    while right < bins - 1 and right - peak_idx < max_right:
        probe = right + 1
        active = combined[probe] >= sustain_thr * 0.96 or motion[probe] >= motion_thr
        right = probe
        if active:
            quiet = 0
        else:
            quiet += 1
            if quiet >= _quiet_bins_to_end():
                break

    region_start = left * win
    region_end = min(file_dur, (right + 1) * win)

    if until_end:
        start = max(0.0, float(peak_sec) - lead)
        end = max(region_end, float(peak_sec) + min_d)
    else:
        region_dur = max(min_d, region_end - region_start)
        start = max(0.0, min(region_start, float(peak_sec) - lead))
        end = min(file_dur, max(start + region_dur, float(peak_sec) + (region_dur - lead)))

    dur = end - start
    if dur < min_d:
        end = min(file_dur, start + min_d)
        dur = end - start

    if max_d > 0 and dur > max_d:
        if until_end:
            end = min(file_dur, start + max_d)
            dur = end - start
        else:
            half = max_d / 2.0
            start = max(0.0, float(peak_sec) - half)
            end = min(file_dur, start + max_d)
            start = max(0.0, end - max_d)
            dur = end - start

    start, dur = apply_head_trim(start, dur, file_dur)
    end = start + dur
    return round(start, 2), round(end, 2), round(dur, 2)


def variable_length_enabled() -> bool:
    return os.environ.get("MLBB_VOD_VARIABLE_LENGTH", "1") == "1"
