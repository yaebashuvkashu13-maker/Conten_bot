#!/usr/bin/env python3
"""MLBB fight-boundary segmentation — variable clip length from combat sustain."""

from __future__ import annotations

import os
from pathlib import Path

import numpy as np


def _fight_min_sec() -> float:
    return float(os.environ.get("MLBB_FIGHT_MIN_SEC", "7"))


def _fight_max_sec() -> float:
    return float(os.environ.get("MLBB_FIGHT_MAX_SEC", "55"))


def _fight_hard_max_sec() -> float:
    return float(os.environ.get("MLBB_FIGHT_HARD_MAX_SEC", "65"))


def _sustain_quiet_bins() -> int:
    return int(os.environ.get("MLBB_FIGHT_SUSTAIN_QUIET_BINS", "3"))


def _extend_bins(max_d: float, win: float) -> int:
    return int(os.environ.get("MLBB_FIGHT_EXTEND_BINS", str(int(max_d / max(win, 0.5)) + 6)))


def _lead_sec() -> float:
    return float(os.environ.get("MLBB_VOD_LEAD_SEC", "4"))


def banner_lead_sec(banner_tier: int | None = None) -> float:
    """Pre-roll before kill banner — Double+ needs fight context, not banner-first."""
    base = _lead_sec()
    tier = int(banner_tier or 0)
    if tier >= 5:
        return float(os.environ.get("MLBB_SAVAGE_BANNER_LEAD_SEC", str(base + 10.0)))
    if tier >= 4:
        return float(os.environ.get("MLBB_MANIAC_BANNER_LEAD_SEC", str(base + 6.0)))
    if tier >= 2:
        # Owner: last clips opened on banner — need ~12–14s of engage before Double/Triple.
        return float(
            os.environ.get(
                "MLBB_DOUBLE_BANNER_LEAD_SEC",
                os.environ.get("MLBB_KILL_BANNER_LEAD_SEC", str(max(base, 14.0))),
            )
        )
    return base


def _fight_post_sec() -> float:
    """Seconds of gameplay after fight sustain ends (viewer outro)."""
    return float(os.environ.get("MLBB_FIGHT_POST_SEC", "4"))


def ideal_clip_min_sec() -> float:
    """Minimum clip: lead before fight + fight body + post after fight."""
    return _lead_sec() + _fight_min_sec() + _fight_post_sec()


def vod_tail_exclude_sec() -> float:
    """No clips from the last N seconds — rank-up, results, outro menus."""
    return float(os.environ.get("MLBB_VOD_TAIL_EXCLUDE_SEC", "75"))


def _vod_duration(vod: Path) -> float:
    p = Path(vod)
    if not p.is_file():
        return 0.0
    try:
        analysis = _analysis_for(p)
        dur = float(analysis.get("duration") or 0.0)
        if dur > 0:
            return dur
    except Exception:
        pass
    import subprocess

    proc = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(p),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    try:
        return float((proc.stdout or "0").strip())
    except ValueError:
        return 0.0


def banner_in_vod_tail(vod: Path, banner_sec: float) -> bool:
    dur = _vod_duration(vod)
    if dur <= 0:
        return False
    return float(banner_sec) >= dur - vod_tail_exclude_sec()


def clip_in_vod_tail(vod: Path, start_sec: float, dur_sec: float) -> bool:
    dur = _vod_duration(vod)
    if dur <= 0:
        return False
    margin = vod_tail_exclude_sec()
    end = float(start_sec) + float(dur_sec)
    if float(start_sec) >= dur - margin:
        return True
    # Clip must not extend into the absolute file tail (rank promo / menu).
    return end > dur - min(20.0, margin * 0.3)


_CACHE: dict[str, dict] = {}


def _analysis_for(vod: Path) -> dict:
    """One analyze_video pass per VOD file — disk cache + in-process memo."""
    from vod_analysis_cache import analyze_video_cached, cache_key_hash

    key = cache_key_hash(vod)
    if key not in _CACHE:
        _CACHE[key] = analyze_video_cached(vod)
    return _CACHE[key]


def clear_analysis_cache() -> None:
    _CACHE.clear()


def detect_fight_bounds(vod: Path, peak_sec: float) -> tuple[float, float, float]:
    """
    Detect fight window around peak_sec.

    Returns (start_sec, end_sec, duration_sec) for the full fight sustain region.
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
    post = _fight_post_sec()
    fight_body = max(min_d, region_end - region_start)

    start = max(0.0, region_start - lead)
    end = min(file_dur, region_end + post)
    dur = end - start

    if dur < ideal_clip_min_sec():
        end = min(file_dur, start + ideal_clip_min_sec())
        dur = end - start
    if fight_body < min_d:
        end = min(file_dur, max(end, region_start + min_d + post))
        dur = end - start

    hard_max = _fight_hard_max_sec()
    if dur > hard_max:
        # Absolute safety cap only — prefer full teamfight up to hard_max.
        end = min(file_dur, region_end)
        start = max(0.0, end - hard_max)
        dur = end - start
    elif dur > max_d and os.environ.get("MLBB_FIGHT_TRIM_LONG", "0") == "1":
        end = min(file_dur, region_end)
        start = max(0.0, end - max_d)
        dur = end - start

    return round(start, 2), round(end, 2), round(dur, 2)


def variable_length_enabled() -> bool:
    return os.environ.get("MLBB_VOD_VARIABLE_LENGTH", "1") == "1"


def clip_active_gameplay_ok(
    vod: Path,
    start_sec: float,
    dur_sec: float,
    *,
    crop_box: tuple[int, int, int, int] | None = None,
) -> tuple[bool, str]:
    """
    Reject clips where the hero is dead/idle for most of the window (tavern / death screen).
    Samples the FULL clip — not only the tail.
    """
    dur = float(dur_sec)
    if dur < 2.5:
        return False, "clip_too_short"

    from gameplay_gate import (
        score_segment_combat,
        segment_hud_frame_pass_rate,
        segment_looks_like_draft_or_queue,
    )

    if segment_looks_like_draft_or_queue(vod, start_sec, dur, crop_box=crop_box):
        return False, "draft_or_queue"

    if clip_in_vod_tail(vod, start_sec, dur):
        return False, f"vod_tail start={start_sec:.1f}"

    from gameplay_gate import segment_looks_like_rank_promo

    if segment_looks_like_rank_promo(vod, start_sec, dur, crop_box=crop_box):
        return False, "rank_promo_or_menu"

    samples = max(6, int(os.environ.get("MLBB_CLIP_COMBAT_SAMPLES", "10")))
    min_active = float(os.environ.get("MLBB_CLIP_MIN_ACTIVE_RATIO", "0.40"))
    min_motion = float(os.environ.get("MLBB_CLIP_WINDOW_MIN_MOTION", "0.018"))
    min_mini = float(os.environ.get("MLBB_CLIP_WINDOW_MIN_MINIMAP", "0.010"))
    min_skill = float(os.environ.get("MLBB_CLIP_WINDOW_MIN_SKILL", "0.006"))
    min_hud = float(os.environ.get("MLBB_CLIP_MIN_HUD_RATE", "0.42"))
    mini_active = min_mini * float(os.environ.get("MLBB_CLIP_MINI_ACTIVE_MULT", "2.2"))

    window = max(1.2, dur / max(samples, 1))
    active_windows = 0
    total_windows = 0
    for i in range(samples):
        if samples == 1:
            t0 = start_sec
        else:
            t0 = start_sec + i * max(0.0, dur - window) / (samples - 1)
        motion, mini, skill, _text = score_segment_combat(
            vod,
            t0,
            window,
            crop_box=crop_box,
            sample_frames=4,
        )
        total_windows += 1
        window_active = (
            motion >= min_motion
            or mini >= mini_active
            or skill >= min_skill * 1.4
        )
        if window_active:
            active_windows += 1

    hud_rate = segment_hud_frame_pass_rate(
        vod, start_sec, dur, crop_box=crop_box, sample_frames=samples
    )
    active_ratio = active_windows / max(total_windows, 1)
    if hud_rate < min_hud:
        return False, f"death_or_tavern hud={hud_rate:.2f} need>={min_hud}"
    if active_ratio < min_active:
        return False, f"idle_clip ratio={active_ratio:.2f} need>={min_active}"
    return True, f"active_ok ratio={active_ratio:.2f} hud={hud_rate:.2f}"


def clip_action_sustain_ok(
    vod: Path,
    start_sec: float,
    dur_sec: float,
    *,
    crop_box: tuple[int, int, int, int] | None = None,
) -> tuple[bool, str]:
    """Full-clip gameplay check (death/tavern/idle) + tail sustain."""
    ok, reason = clip_active_gameplay_ok(vod, start_sec, dur_sec, crop_box=crop_box)
    if not ok:
        return ok, reason
    dur = float(dur_sec)
    if dur < 6.0:
        return True, reason
    min_tail_motion = float(os.environ.get("MLBB_CLIP_MIN_TAIL_MOTION", "0.016"))
    min_tail_mini = float(os.environ.get("MLBB_CLIP_MIN_TAIL_MINIMAP", "0.007"))
    tail_start = float(start_sec) + dur * 0.42
    tail_dur = max(2.5, float(start_sec) + dur - tail_start)
    from gameplay_gate import score_segment_combat

    motion, mini, _skill, _text = score_segment_combat(
        vod,
        tail_start,
        tail_dur,
        crop_box=crop_box,
        sample_frames=max(4, int(os.environ.get("MLBB_CLIP_TAIL_SAMPLES", "5"))),
    )
    if motion < min_tail_motion and mini < min_tail_mini:
        return False, f"idle_death_tail motion={motion:.4f} mini={mini:.4f}"
    return True, reason
