#!/usr/bin/env python3
"""Reject PUBG clips that are mostly running or fight-at-end — owner wants gunfight only."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any


def max_prefight_lead_frac() -> float:
    return float(os.environ.get("PUBG_CLIP_MAX_LEAD_FRAC", "0.12"))


def max_peak_position_frac() -> float:
    return float(os.environ.get("PUBG_CLIP_MAX_PEAK_FRAC", "0.65"))


def min_gunfire_coverage_frac() -> float:
    return float(os.environ.get("PUBG_CLIP_MIN_GUN_COVERAGE", "0.42"))


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
        if tail > float(os.environ.get("PUBG_CLIP_MAX_POST_FIGHT_SEC", "3.5")):
            return False, f"loot_tail tail={tail:.1f}s"

    # Head/tail run: even if the fight core is hot, lead/tail sprint looks like loot_run.
    ht_reason = _head_tail_run_reason(report, start_f, dur_f)
    if ht_reason:
        return False, ht_reason

    coverage = _gunfire_coverage(report, start_f, dur_f)
    if coverage is not None and 0.0 < coverage < min_gunfire_coverage_frac():
        return False, f"low_gun_coverage={coverage:.2f}"

    # Reject clips that cut mid-burst (owner: ends on shooting).
    try:
        from pubg_montage_bounds import clip_ends_on_gunfire

        if clip_ends_on_gunfire(start_f, dur_f, report):
            return False, "ends_on_gunfire"
    except Exception:
        pass

    return True, "fight_shape_ok"



def _head_tail_run_reason(
    report: dict[str, Any],
    start: float,
    dur: float,
) -> str | None:
    """Reject clips whose first/last seconds are quiet run while the middle is the fight."""
    timeline = report.get("timeline")
    if not isinstance(timeline, list) or not timeline:
        return None
    edge = float(os.environ.get("PUBG_CLIP_EDGE_RUN_SEC", "3.5"))
    gun_min = float(os.environ.get("PUBG_SEGMENT_GUN_ONSET_MIN", "0.025"))
    max_edge_gun = float(os.environ.get("PUBG_CLIP_EDGE_MAX_GUN", "0.018"))
    if dur < edge * 2.5:
        return None
    end = start + dur
    head_end = start + edge
    tail_start = end - edge

    def _avg_gun(a: float, b: float) -> float | None:
        vals: list[float] = []
        for row in timeline:
            try:
                t = float(row.get("start", 0))
            except (TypeError, ValueError):
                continue
            if a <= t < b:
                vals.append(float(row.get("gun", 0.0) or 0.0))
        if not vals:
            return None
        return sum(vals) / len(vals)

    head = _avg_gun(start, head_end)
    tail = _avg_gun(tail_start, end)
    mid = _avg_gun(head_end, tail_start)
    if mid is None or mid < gun_min:
        return None
    if head is not None and head <= max_edge_gun and mid >= gun_min * 1.5:
        return f"head_run gun={head:.3f}"
    if tail is not None and tail <= max_edge_gun and mid >= gun_min * 1.5:
        return f"tail_run gun={tail:.3f}"
    return None


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
    end = start + dur
    gun_bins = 0
    total_bins = 0
    for row in timeline:
        t = float(row.get("start", 0))
        if t + step < start or t > end:
            continue
        total_bins += 1
        # Gun-only — loud loot/run score must not inflate coverage.
        if float(row.get("gun", 0.0) or 0.0) >= gun_min:
            gun_bins += 1
    if total_bins <= 0:
        return None
    return gun_bins / total_bins


def aggressive_tighten_for_shape(
    start: float,
    dur: float,
    peak: float,
    report: dict[str, Any],
    *,
    single: bool = False,
) -> tuple[float, float]:
    """Trim to gunfire window when shape gate would fail."""
    from pubg_montage_bounds import (
        clip_post_kill_sec,
        clip_pre_shoot_sec,
        extend_end_past_active_gunfire,
        _gun_bin_active,
    )

    shoot = report.get("shooting_start")
    if shoot is None:
        return start, dur
    pre = min(clip_pre_shoot_sec(), float(os.environ.get("PUBG_CLIP_MAX_PRE_SHOOT_SEC", "1.2")))
    post = clip_post_kill_sec()
    start = float(shoot) - pre
    kill = report.get("kill_sec") if report.get("kill_sec") is not None else report.get("kill_time")
    fight_end = report.get("fight_end") or report.get("fight_end_sec")
    end = float(start) + float(dur)
    gun_continues = False
    if kill is not None and isinstance(report.get("timeline"), list):
        for row in report["timeline"]:
            try:
                t = float(row["start"])
            except (KeyError, TypeError, ValueError):
                continue
            if float(kill) + 1.0 <= t <= float(kill) + 10.0 and _gun_bin_active(row):
                gun_continues = True
                break
    # Singles / continuing fights: keep full fight, never crush to kill+post mid-burst.
    if kill is not None and not (single or gun_continues):
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
    if not ok and not single and not gun_continues:
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
    max_dur = float(
        os.environ.get("PUBG_SINGLE_MAX_SEC", "90")
        if single
        else os.environ.get("PUBG_SEGMENT_MAX_SEC", "55")
    )
    start, dur = extend_end_past_active_gunfire(
        start, dur, report, max_dur=max_dur, single=single
    )
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
