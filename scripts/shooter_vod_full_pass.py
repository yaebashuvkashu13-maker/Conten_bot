#!/usr/bin/env python3
"""One-pass PUBG/Standoff scan for short VODs (3–20 min) — gunfire curve + dense peaks."""

from __future__ import annotations

import logging
import os
from pathlib import Path

import numpy as np

from highlight_scorer import (
    WINDOW_SEC,
    _filter_bad_label_starts,
    _owner_anchor_stage1_starts,
    _owner_anchor_starts,
    _rank_stage1_starts,
    normalize_profile,
    owner_anchors_enabled,
    soft_anchor_enabled,
)

log = logging.getLogger("shooter_vod_full_pass")


def full_pass_enabled() -> bool:
    return os.environ.get("SHOOTER_VOD_FULL_PASS", "1") == "1"


def full_pass_max_sec() -> float:
    return float(os.environ.get("SHOOTER_VOD_FULL_PASS_MAX_SEC", "1200"))


def shooter_skip_intro_sec(duration: float) -> float:
    """Short Metro VODs have fights from ~0:30 — do not skip the whole first half."""
    dur = max(0.0, float(duration))
    if dur <= 180:
        return float(os.environ.get("SHOOTER_VOD_SHORT_SKIP_INTRO", "8"))
    if dur <= 360:
        return float(os.environ.get("SHOOTER_VOD_MED_SKIP_INTRO", "20"))
    if dur <= 720:
        return float(os.environ.get("SHOOTER_VOD_MID_SKIP_INTRO", "45"))
    return float(os.environ.get("SHOOTER_VOD_SKIP_INTRO_SEC", "90"))


def peak_min_gap_sec(duration: float) -> float:
    dur = max(0.0, float(duration))
    if dur <= 240:
        return float(os.environ.get("SHOOTER_VOD_SHORT_PEAK_GAP_SEC", "22"))
    if dur <= 600:
        return float(os.environ.get("SHOOTER_VOD_MED_PEAK_GAP_SEC", "35"))
    return float(os.environ.get("HIGHLIGHT_PEAK_MIN_GAP_SEC", "75"))


def _combined_action(analysis: dict) -> np.ndarray:
    gun = np.asarray(analysis.get("gunfire", analysis["audio"]), dtype=np.float32)
    motion = np.asarray(analysis["center_motion"], dtype=np.float32)
    audio = np.asarray(analysis["audio"], dtype=np.float32)
    return gun * 0.62 + motion * 0.22 + audio * 0.16


def _dense_gunfire_starts(analysis: dict, duration: float) -> list[float]:
    win = float(analysis.get("window_seconds", 2.0))
    combined = _combined_action(analysis)
    if combined.size == 0:
        return []
    skip = shooter_skip_intro_sec(duration)
    step = float(os.environ.get("SHOOTER_VOD_FULL_PASS_STEP_SEC", "2"))
    tail_pad = float(os.environ.get("SHOOTER_VOD_FULL_PASS_TAIL_PAD_SEC", "5"))
    pctl = float(os.environ.get("SHOOTER_VOD_FULL_PASS_GUN_PCTL", "68"))
    floor = float(os.environ.get("SHOOTER_VOD_FULL_PASS_GUN_FLOOR", "0.018"))
    threshold = max(floor, float(np.percentile(combined, pctl)))

    starts: set[float] = set()
    t = skip
    while t + WINDOW_SEC <= duration - tail_pad:
        i0 = max(0, int(t / win))
        i1 = min(len(combined), max(i0 + 1, int((t + WINDOW_SEC) / win)))
        if float(np.max(combined[i0:i1])) >= threshold:
            start = round(t, 1)
            try:
                from vod_scan_state import peak_in_exclude_intervals

                if not peak_in_exclude_intervals(start):
                    starts.add(start)
            except ImportError:
                starts.add(start)
        t += step
    return sorted(starts)


def _spread_peaks(analysis: dict, duration: float, *, limit: int) -> list[float]:
    win = float(analysis.get("window_seconds", 2.0))
    combined = _combined_action(analysis)
    if combined.size == 0:
        return []
    skip = shooter_skip_intro_sec(duration)
    min_gap = peak_min_gap_sec(duration)
    order = np.argsort(combined)[::-1]
    starts: list[float] = []
    for idx in order:
        start = float(idx) * win
        if start < skip or start + WINDOW_SEC > duration - 5:
            continue
        if any(abs(start - s) < min_gap for s in starts):
            continue
        try:
            from vod_scan_state import peak_in_exclude_intervals

            if peak_in_exclude_intervals(start):
                continue
        except ImportError:
            pass
        starts.append(round(start, 1))
        if len(starts) >= limit:
            break
    return starts


def stage1_shooter_full_pass(video_path: Path, profile: str) -> list[float] | None:
    """
    Build stage1 from one analyze_video pass when VOD is short enough.
    Returns None when full-pass mode is off or VOD is too long.
    """
    profile = normalize_profile(profile)
    if not full_pass_enabled():
        return None

    from smart_video_editor import ffprobe_duration

    duration = float(ffprobe_duration(video_path) or 0.0)
    if duration <= 0 or duration > full_pass_max_sec():
        return None

    from vod_analysis_cache import analyze_video_cached

    analysis = analyze_video_cached(video_path)
    peak_limit = int(os.environ.get("SHOOTER_VOD_FULL_PASS_PEAK_LIMIT", "28"))
    max_stage1 = int(os.environ.get("HIGHLIGHT_MAX_STAGE1", "48"))

    starts: set[float] = set(_dense_gunfire_starts(analysis, duration))
    for peak in _spread_peaks(analysis, duration, limit=peak_limit):
        starts.add(peak)

    if owner_anchors_enabled():
        for anchor_start in _owner_anchor_stage1_starts(video_path, profile):
            starts.add(anchor_start)

    if soft_anchor_enabled(video_path, profile):
        for anchor in _owner_anchor_starts(video_path, profile):
            for off in (-45, -20, 0, 20, 45):
                s = anchor + off - WINDOW_SEC * 0.5
                if s >= shooter_skip_intro_sec(duration) - 5:
                    starts.add(round(max(0.0, s), 1))

    ranked = _rank_stage1_starts(analysis, profile, sorted(starts))
    if not ranked:
        ranked = sorted(starts)
    ranked = _filter_bad_label_starts(video_path, profile, ranked)
    out = ranked[:max_stage1]
    log.info(
        "shooter full-pass stage1 %s: dur=%.0fs skip=%.0fs windows=%s peaks=%s",
        video_path.name,
        duration,
        shooter_skip_intro_sec(duration),
        len(out),
        len(starts),
    )
    return out
