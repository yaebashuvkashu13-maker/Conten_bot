#!/usr/bin/env python3
"""MLBB fight-boundary segmentation — variable clip length from combat sustain."""

from __future__ import annotations

import os
from pathlib import Path

import numpy as np


def _fight_min_sec() -> float:
    return float(os.environ.get("MLBB_FIGHT_MIN_SEC", "7"))


def _fight_max_sec() -> float:
    return float(os.environ.get("MLBB_FIGHT_MAX_SEC", "60"))


def _fight_hard_max_sec() -> float:
    return float(os.environ.get("MLBB_FIGHT_HARD_MAX_SEC", "75"))


def _sustain_quiet_bins() -> int:
    return int(os.environ.get("MLBB_FIGHT_SUSTAIN_QUIET_BINS", "3"))


def _extend_bins(max_d: float, win: float) -> int:
    return int(os.environ.get("MLBB_FIGHT_EXTEND_BINS", str(int(max_d / max(win, 0.5)) + 6)))


def _lead_sec() -> float:
    """Default pre-roll before peak/banner (prefer kill-banner lead, not short 2–4s)."""
    return float(
        os.environ.get(
            "MLBB_VOD_LEAD_SEC",
            os.environ.get("MLBB_KILL_BANNER_LEAD_SEC", "12"),
        )
    )


def banner_lead_sec(banner_tier: int | None = None) -> float:
    """Pre-roll before kill banner — double/triple/maniac/savage need prior kills visible."""
    base = float(
        os.environ.get(
            "MLBB_KILL_BANNER_LEAD_SEC",
            os.environ.get("MLBB_VOD_LEAD_SEC", "12"),
        )
    )
    # Explicit BANNER_PRE_SEC only if >= base (never shrink lead for doubles).
    pre_raw = (os.environ.get("MLBB_BANNER_PRE_SEC") or "").strip()
    if pre_raw:
        try:
            base = max(base, float(pre_raw))
        except ValueError:
            pass
    tier = int(banner_tier or 0)

    def _tier_lead(env_key: str, default_extra: float) -> float:
        # Never allow tier-specific overrides to shrink below the global lead.
        raw = (os.environ.get(env_key) or "").strip()
        if raw:
            try:
                return max(base, float(raw))
            except ValueError:
                pass
        return base + default_extra

    if tier >= 5:
        # Savage: full streak (kills 1→5). 22s often lands on the triple.
        return _tier_lead("MLBB_SAVAGE_BANNER_LEAD_SEC", 24.0)
    if tier >= 4:
        # Maniac: prior kills before the 4th banner, not mid-combo.
        return _tier_lead("MLBB_MANIAC_BANNER_LEAD_SEC", 12.0)
    if tier >= 3:
        return _tier_lead("MLBB_TRIPLE_BANNER_LEAD_SEC", 6.0)
    return base


def _fight_post_sec() -> float:
    """Seconds to keep after the last kill banner — short to avoid post-fight running."""
    return float(
        os.environ.get(
            "MLBB_BANNER_POST_SEC",
            os.environ.get("MLBB_FIGHT_POST_SEC", "3"),
        )
    )


def banner_post_sec() -> float:
    """Public alias: cut highlight this many seconds after the kill banner."""
    return _fight_post_sec()


def ideal_clip_min_sec(banner_tier: int | None = None) -> float:
    """Minimum clip length — keep singles compact; streak banners keep longer pre-roll."""
    tier = int(banner_tier or 0)
    lead = banner_lead_sec(tier) if tier > 0 else _lead_sec()
    post = _fight_post_sec()
    # Tier 1–2: do not pad to lead+fight_min+post (~22s) — that created 18s idle heads
    # before a single kill (AJxzNqHrlyo_294).
    if tier <= 2:
        return float(os.environ.get("MLBB_SINGLE_IDEAL_MIN_SEC", str(lead + post + 1.0)))
    return lead + _fight_min_sec() + post


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
    region_dur = max(min_d, region_end - region_start)

    start = max(0.0, min(region_start, float(peak_sec) - lead))
    end = min(file_dur, max(start + region_dur, float(peak_sec) + (region_dur - lead)))
    dur = end - start

    if dur < min_d:
        end = min(file_dur, start + min_d)
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
