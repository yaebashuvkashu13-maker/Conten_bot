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


def _owner_anchor_radius() -> float:
    return float(os.environ.get("PUBG_OWNER_ANCHOR_RADIUS_SEC", "12"))


def snap_peak_to_owner_label(vod: Path, peak_sec: float) -> tuple[float, bool]:
    """On calibrated VODs use the owner's exact timestamp, not a nearby detector peak."""
    if os.environ.get("SHOOTER_VOD_OWNER_ANCHOR_PEAK", "1") != "1":
        return peak_sec, False
    try:
        from pubg_owner_calibration import labels_for_video, nearest_owner_label
    except ImportError:
        return peak_sec, False
    label, dist = nearest_owner_label(vod, peak_sec, radius_sec=_owner_anchor_radius())
    if label != "good" or dist > _owner_anchor_radius():
        return peak_sec, False
    for row in labels_for_video(vod):
        if row.get("label") != "good":
            continue
        if abs(float(row["time_sec"]) - peak_sec) <= _owner_anchor_radius():
            return float(row["time_sec"]), True
    return peak_sec, False


def _file_duration(vod: Path) -> float:
    from vod_analysis_cache import analyze_video_cached

    analysis = analyze_video_cached(vod)
    file_dur = float(analysis.get("duration") or 0.0)
    if file_dur <= 0:
        from mlbb_vod_segment_feed import _ffprobe_duration

        file_dur = float(_ffprobe_duration(vod) or 0.0)
    return file_dur


def _fight_end_after_peak(
    peak_sec: float,
    *,
    start: float,
    file_dur: float,
    gun: np.ndarray,
    win: float,
) -> float:
    """Extend clip end from peak forward using gunfire tail (owner-pinned start)."""
    tail_pad = _tail_pad_sec()
    max_d = _max_sec()
    min_d = _min_sec()
    gun_thr = max(0.018, float(np.percentile(gun, 55)) * 0.65) if gun.size else 0.02
    peak_idx = max(0, min(len(gun) - 1, int(round(float(peak_sec) / win))))
    last_gun = peak_idx
    quiet = 0
    right = peak_idx
    while right < len(gun) - 1 and (right - peak_idx) < int(max_d / win) + 4:
        nxt = right + 1
        if gun[nxt] >= gun_thr:
            last_gun = nxt
            quiet = 0
        else:
            quiet += 1
            if quiet >= _tail_quiet_bins() + 1:
                break
        right = nxt
    region_end = min(file_dur, (last_gun + 1) * win + tail_pad)
    end = min(file_dur, max(region_end, start + min_d))
    if end - start > max_d:
        end = min(file_dur, start + max_d)
    return end


def detect_pubg_fight_bounds(
    vod: Path,
    peak_sec: float,
    *,
    owner_pinned: bool = False,
) -> tuple[float, float, float]:
    """
    Variable-length PUBG clip: start before peak, end after last gunfire + tail pad.

    owner_pinned: do not expand start left of peak-lead (owner calibration cut).
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
        file_dur = _file_duration(vod)

    gun = np.asarray(analysis.get("gunfire", analysis.get("audio", [])), dtype=np.float32)
    motion = np.asarray(analysis.get("center_motion", []), dtype=np.float32)
    bins = int(len(gun))
    if bins < 2 or file_dur <= 0:
        start = max(0.0, float(peak_sec) - lead)
        end = min(file_dur, start + min(max_d, 15.0))
        return round(start, 2), round(end, 2), round(end - start, 2)

    if owner_pinned:
        start = max(0.0, float(peak_sec) - lead)
        gun_end = _fight_end_after_peak(
            peak_sec,
            start=start,
            file_dur=file_dur,
            gun=gun,
            win=win,
        )
        end = min(file_dur, max(gun_end, start + max_d))
        dur = end - start
        return round(start, 2), round(end, 2), round(dur, 2)

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
    owner_pinned = bool(clip.get("owner_label_cut") or clip.get("owner_pinned"))
    if not owner_pinned:
        peak, owner_pinned = snap_peak_to_owner_label(vod, peak)
    start, end, dur = detect_pubg_fight_bounds(vod, peak, owner_pinned=owner_pinned)
    if dur < _min_sec() * 0.5:
        return clip
    return {
        **clip,
        "start": start,
        "peak_start": peak,
        "fight_end": end,
        "input_duration": dur,
        "output_duration": dur,
        "owner_pinned": owner_pinned,
    }
