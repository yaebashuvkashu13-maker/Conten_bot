#!/usr/bin/env python3
"""PUBG fight window — extend clip past last gunfire (no global time shift)."""

from __future__ import annotations

import os
from pathlib import Path

import numpy as np


def _lead_sec() -> float:
    return float(os.environ.get("MLBB_VOD_LEAD_SEC", "4"))


def _min_sec() -> float:
    return float(os.environ.get("PUBG_FIGHT_MIN_SEC", "8"))


def _max_sec() -> float:
    return float(os.environ.get("PUBG_FIGHT_MAX_SEC", "45"))


def _tail_pad_sec() -> float:
    """Keep filming N seconds after gunfire fades (reposition, loot, killfeed)."""
    return float(os.environ.get("PUBG_FIGHT_TAIL_PAD_SEC", "6"))


def _tail_quiet_bins() -> int:
    """Bins of quiet gun channel before we stop extending (≈2s/bin)."""
    return int(os.environ.get("PUBG_FIGHT_TAIL_QUIET_BINS", "2"))


def detect_pubg_fight_bounds(vod: Path, peak_sec: float) -> tuple[float, float, float]:
    """
    Variable-length PUBG clip: start before peak, end after last gunfire + tail pad.

    Does NOT shift all VODs by a fixed offset — only lengthens the tail.
    """
    from vod_analysis_cache import analyze_video_cached

    lead = _lead_sec()
    min_d = _min_sec()
    max_d = _max_sec()
    tail_pad = _tail_pad_sec()

    analysis = analyze_video_cached(vod)
    win = float(analysis.get("window_seconds", 2.0))
    file_dur = float(analysis.get("duration") or 0.0)
    if file_dur <= 0:
        from mlbb_vod_segment_feed import _ffprobe_duration

        file_dur = float(_ffprobe_duration(vod) or 0.0)

    gun = np.asarray(analysis.get("gunfire", analysis.get("audio", [])), dtype=np.float32)
    motion = np.asarray(analysis.get("center_motion", []), dtype=np.float32)
    bins = int(len(gun))
    if bins < 2 or file_dur <= 0:
        start = max(0.0, float(peak_sec) - lead)
        end = min(file_dur, start + min(max_d, 15.0))
        return round(start, 2), round(end, 2), round(end - start, 2)

    gun_thr = max(0.018, float(np.percentile(gun, 55)) * 0.65)
    motion_thr = max(0.012, float(np.percentile(motion, 50)) * 0.55) if motion.size else 0.012
    active = (gun >= gun_thr) | (motion >= motion_thr)

    peak_idx = max(0, min(bins - 1, int(round(float(peak_sec) / win))))

    left = peak_idx
    quiet = 0
    while left > 0 and peak_idx - left < int(max_d / win) + 2:
        if active[left - 1]:
            left -= 1
            quiet = 0
        else:
            quiet += 1
            if quiet >= _tail_quiet_bins():
                break
            left -= 1

    right = peak_idx
    last_gun = peak_idx
    quiet = 0
    while right < bins - 1 and (right - peak_idx) < int(max_d / win) + 4:
        nxt = right + 1
        if gun[nxt] >= gun_thr:
            last_gun = nxt
            quiet = 0
        elif active[nxt]:
            quiet = 0
        else:
            quiet += 1
            if quiet >= _tail_quiet_bins() + 1:
                break
        right = nxt

    region_start = max(0.0, left * win)
    region_end = min(file_dur, (last_gun + 1) * win + tail_pad)

    start = max(0.0, min(region_start, float(peak_sec) - lead))
    end = min(file_dur, max(region_end, start + min_d))
    dur = end - start

    if dur > max_d:
        end = min(file_dur, start + max_d)
        dur = end - start

    return round(start, 2), round(end, 2), round(dur, 2)


def apply_fight_bounds_to_clip(clip: dict, vod: Path) -> dict:
    if os.environ.get("SHOOTER_VOD_VARIABLE_LENGTH", "1") != "1":
        return clip
    peak = float(clip.get("peak_start", clip.get("start", 0)))
    start, end, dur = detect_pubg_fight_bounds(vod, peak)
    if dur < _min_sec() * 0.5:
        return clip
    return {
        **clip,
        "start": start,
        "peak_start": peak,
        "fight_end": end,
        "input_duration": dur,
        "output_duration": dur,
    }
