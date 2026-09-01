#!/usr/bin/env python3
"""Reject PUBG clips that are mostly running or fight-at-end — owner wants gunfight only."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any


def max_prefight_lead_frac() -> float:
    return float(os.environ.get("PUBG_CLIP_MAX_LEAD_FRAC", "0.20"))


def max_peak_position_frac() -> float:
    return float(os.environ.get("PUBG_CLIP_MAX_PEAK_FRAC", "0.78"))


def min_gunfire_coverage_frac() -> float:
    return float(os.environ.get("PUBG_CLIP_MIN_GUN_COVERAGE", "0.28"))


def validate_clip_fight_shape(
    start: float,
    dur: float,
    peak: float,
    report: dict[str, Any],
) -> tuple[bool, str]:
    """True when clip is mostly gunfight — not loot-run or payoff-only tail."""
    if dur <= 0:
        return False, "zero_duration"
    shoot = report.get("shooting_start")
    if shoot is None:
        return False, "no_shooting_start"
    shoot_f = float(shoot)
    start_f = float(start)
    dur_f = float(dur)
    peak_f = float(peak)

    lead = shoot_f - start_f
    lead_frac = lead / dur_f
    if lead_frac > max_prefight_lead_frac():
        return False, f"prefight_run lead={lead:.1f}s frac={lead_frac:.2f}"

    peak_frac = (peak_f - start_f) / dur_f
    if peak_frac > max_peak_position_frac():
        return False, f"fight_at_end peak_frac={peak_frac:.2f}"

    fight_end = report.get("fight_end") or report.get("fight_end_sec")
    if fight_end is not None:
        tail = start_f + dur_f - float(fight_end)
        if tail > float(os.environ.get("PUBG_CLIP_MAX_POST_FIGHT_SEC", "6.0")):
            return False, f"loot_tail tail={tail:.1f}s"

    coverage = _gunfire_coverage(report, start_f, dur_f)
    if coverage is not None and 0.0 < coverage < min_gunfire_coverage_frac():
        return False, f"low_gun_coverage={coverage:.2f}"

    return True, "fight_shape_ok"


def _gunfire_coverage(
    report: dict[str, Any],
    start: float,
    dur: float,
) -> float | None:
    timeline = report.get("timeline")
    if not isinstance(timeline, list) or not timeline:
        return None
    step = float(os.environ.get("PUBG_SEGMENT_BIN_SEC", "2"))
    gun_min = float(os.environ.get("PUBG_SEGMENT_GUN_ONSET_MIN", "0.025"))
    active_min = float(os.environ.get("PUBG_SEGMENT_ACTIVITY_MIN", "0.34"))
    end = start + dur
    gun_bins = 0
    total_bins = 0
    for row in timeline:
        t = float(row.get("start", 0))
        if t + step < start or t > end:
            continue
        total_bins += 1
        if float(row.get("gun", 0.0) or 0.0) >= gun_min or float(row.get("score", 0.0) or 0.0) >= active_min:
            gun_bins += 1
    if total_bins <= 0:
        return None
    return gun_bins / total_bins


def aggressive_tighten_for_shape(
    start: float,
    dur: float,
    peak: float,
    report: dict[str, Any],
) -> tuple[float, float]:
    """Trim to gunfire window when shape gate would fail."""
    from pubg_montage_bounds import clip_post_kill_sec, clip_pre_shoot_sec

    shoot = report.get("shooting_start")
    if shoot is None:
        return start, dur
    pre = min(clip_pre_shoot_sec(), float(os.environ.get("PUBG_CLIP_MAX_PRE_SHOOT_SEC", "1.2")))
    post = clip_post_kill_sec()
    start = float(shoot) - pre
    kill = report.get("kill_sec") if report.get("kill_sec") is not None else report.get("kill_time")
    fight_end = report.get("fight_end") or report.get("fight_end_sec")
    end = float(start) + float(dur)
    if kill is not None:
        end = min(end, float(kill) + post)
    if fight_end is not None:
        end = min(end, float(fight_end))
    # Keep peak inside clip when possible.
    if peak < start:
        start = max(0.0, float(peak) - pre)
    if peak > end:
        end = float(peak) + post
    dur = max(8.0, end - start)
    ok, _reason = validate_clip_fight_shape(start, dur, peak, report)
    if not ok:
        want = min(float(dur), float(os.environ.get("PUBG_CLIP_TARGET_FIGHT_SEC", "16")))
        start = max(0.0, float(shoot) - want * 0.35)
        end = start + want
        if kill is not None:
            end = min(end, float(kill) + post)
        dur = max(8.0, end - start)
        rel_peak = (float(peak) - start) / max(dur, 1.0)
        if rel_peak > max_peak_position_frac():
            start = max(0.0, float(peak) - dur * 0.42)
            dur = max(8.0, end - start)
    return float(start), float(dur)


def probe_clip_shape_live(
    vod: Path,
    start: float,
    dur: float,
    peak: float,
) -> tuple[bool, str, dict[str, Any]]:
    """Live gunfire probe when segment report missing."""
    from pubg_fight_segment import resolve_pubg_fight_bounds
    from shooter_vod_segment_feed import _ffprobe_duration

    _s, _d, report = resolve_pubg_fight_bounds(vod, peak, file_duration=_ffprobe_duration(vod))
    ok, reason = validate_clip_fight_shape(start, dur, peak, report)
    return ok, reason, report


__all__ = [
    "aggressive_tighten_for_shape",
    "probe_clip_shape_live",
    "validate_clip_fight_shape",
]
