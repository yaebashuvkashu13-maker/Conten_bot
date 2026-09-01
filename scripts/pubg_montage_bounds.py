#!/usr/bin/env python3
"""Shared PUBG montage fight-window logic — main feed + owner redo."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any


def min_fight_window_gap_sec() -> float:
    return float(
        os.environ.get(
            "PUBG_MONTAGE_MIN_WINDOW_GAP_SEC",
            os.environ.get("PUBG_OWNER_REDO_MIN_WINDOW_GAP_SEC", "30"),
        )
    )


def clip_pre_shoot_sec() -> float:
    return float(
        os.environ.get(
            "PUBG_CLIP_PRE_SHOOT_SEC",
            os.environ.get("PUBG_OWNER_CLIP_PRE_SHOOT_SEC", "1.5"),
        )
    )


def clip_post_kill_sec() -> float:
    return float(
        os.environ.get(
            "PUBG_CLIP_POST_KILL_SEC",
            os.environ.get("PUBG_OWNER_POST_KILL_SEC", "5.0"),
        )
    )


def fight_bounds(
    vod: Path,
    peak: float,
    file_dur: float | None = None,
) -> tuple[float, float]:
    from pubg_fight_segment import resolve_pubg_fight_bounds

    if file_dur is None:
        from shooter_vod_segment_feed import _ffprobe_duration

        file_dur = _ffprobe_duration(vod)
    start, dur, _report = resolve_pubg_fight_bounds(vod, peak, file_duration=file_dur)
    return float(start), float(start + dur)


def bounds_distinct(a: tuple[float, float], b: tuple[float, float]) -> bool:
    gap = min_fight_window_gap_sec()
    return a[1] + gap <= b[0] or b[1] + gap <= a[0]


def tighten_pubg_clip_bounds(
    start: float,
    dur: float,
    report: dict[str, Any],
    *,
    peak: float | None = None,
) -> tuple[float, float]:
    """Start at gunfire, end soon after kill — no loot-walk tail."""
    from pubg_clip_shape_gate import aggressive_tighten_for_shape, validate_clip_fight_shape

    pre_pad = clip_pre_shoot_sec()
    post_kill = clip_post_kill_sec()
    max_lead = float(os.environ.get("PUBG_CLIP_MAX_PRE_SHOOT_SEC", "1.2"))
    shoot = report.get("shooting_start")
    if shoot is not None:
        start = float(shoot) - min(pre_pad, max_lead)
    kill = report.get("kill_sec") if report.get("kill_sec") is not None else report.get("kill_time")
    end = float(start) + float(dur)
    if kill is not None:
        end = min(end, float(kill) + post_kill)
    fight_end = report.get("fight_end")
    if fight_end is not None:
        end = min(end, float(fight_end))
    dur = max(8.0, end - float(start))
    if peak is not None:
        ok, _reason = validate_clip_fight_shape(start, dur, float(peak), report)
        if not ok:
            start, dur = aggressive_tighten_for_shape(start, dur, float(peak), report)
    return float(start), float(dur)


def peak_fight_report(
    vod: Path,
    peak: float,
    file_dur: float | None = None,
) -> dict[str, Any]:
    from pubg_fight_segment import resolve_pubg_fight_bounds

    if file_dur is None:
        from shooter_vod_segment_feed import _ffprobe_duration

        file_dur = _ffprobe_duration(vod)
    _start, _dur, report = resolve_pubg_fight_bounds(vod, peak, file_duration=file_dur)
    return report


def peak_has_kill(
    vod: Path,
    peak: float,
    file_dur: float | None = None,
) -> bool:
    report = peak_fight_report(vod, peak, file_dur)
    if report.get("kill_sec") is not None or report.get("kill_time") is not None:
        return True
    return float(report.get("killfeed_score", 0.0) or 0.0) >= 0.35


def dedupe_peaks_by_fight_window(
    vod: Path,
    peaks: list[float],
    *,
    file_dur: float | None = None,
) -> list[float]:
    kept: list[float] = []
    bounds: list[tuple[float, float]] = []
    for peak in sorted(peaks):
        window = fight_bounds(vod, peak, file_dur)
        if any(not bounds_distinct(window, prev) for prev in bounds):
            continue
        kept.append(float(peak))
        bounds.append(window)
    return kept


def peak_blocked_by_used_fights(
    vod: Path,
    peak: float,
    used_peaks: list[float],
    *,
    file_dur: float | None = None,
    peak_gap_sec: float = 0.0,
) -> bool:
    """True when peak overlaps a used fight window or is within peak_gap_sec."""
    from vod_peak_gap import peak_too_close

    if peak_too_close(peak, used_peaks, peak_gap_sec):
        return True
    if not used_peaks:
        return False
    window = fight_bounds(vod, peak, file_dur)
    for used in used_peaks:
        if not bounds_distinct(window, fight_bounds(vod, used, file_dur)):
            return True
    return False


def _row_peak(row: dict[str, Any]) -> float:
    return float(row.get("peak_start", row.get("start", 0)) or 0)


def filter_rows_distinct_fights(
    vod: Path,
    rows: list[dict[str, Any]],
    *,
    file_dur: float | None = None,
    max_clips: int | None = None,
) -> list[dict[str, Any]]:
    """Drop montage rows whose trimmed fight windows overlap."""
    if not rows:
        return []
    ranked = sorted(rows, key=lambda row: float(row.get("score", 0) or 0), reverse=True)
    kept: list[dict[str, Any]] = []
    bounds: list[tuple[float, float]] = []
    for row in ranked:
        peak = _row_peak(row)
        window = fight_bounds(vod, peak, file_dur)
        if any(not bounds_distinct(window, prev) for prev in bounds):
            continue
        kept.append(row)
        bounds.append(window)
        if max_clips is not None and len(kept) >= max_clips:
            break
    kept.sort(key=_row_peak)
    return kept


def peak_shape_ok(
    vod: Path,
    peak: float,
    *,
    file_dur: float | None = None,
) -> bool:
    from pubg_clip_shape_gate import validate_clip_fight_shape
    from pubg_fight_segment import resolve_pubg_fight_bounds

    if file_dur is None:
        from shooter_vod_segment_feed import _ffprobe_duration

        file_dur = _ffprobe_duration(vod)
    start, dur, report = resolve_pubg_fight_bounds(vod, peak, file_duration=file_dur)
    start, dur = tighten_pubg_clip_bounds(start, dur, report, peak=float(peak))
    ok, _reason = validate_clip_fight_shape(start, dur, float(peak), report)
    return ok


def select_distinct_kill_peaks(
    vod: Path,
    pool: list[float],
    *,
    min_clips: int = 2,
    max_clips: int = 2,
    file_dur: float | None = None,
    avoid: list[float] | None = None,
) -> list[float]:
    """Pick distinct fights with kill notification — gunfire first, payoff confirmed."""
    from pubg_fast_peak_rank import rank_peaks_fast

    avoid = avoid or []
    filtered = [
        float(p)
        for p in pool
        if not any(abs(float(p) - float(bad)) <= 25.0 for bad in avoid)
    ]
    if len(filtered) < min_clips:
        return []
    ranked, _reason, meta = rank_peaks_fast(vod, filtered, "pubg", part_sec=14.0)
    with_hit = [
        float(p)
        for p in ranked
        if (
            meta.get(float(p), {}).get("notification_hit")
            or peak_has_kill(vod, float(p), file_dur)
        )
        and peak_shape_ok(vod, float(p), file_dur=file_dur)
    ]
    if len(with_hit) < min_clips:
        shaped = [float(p) for p in ranked if peak_shape_ok(vod, float(p), file_dur=file_dur)]
        with_hit = shaped or with_hit
    candidates = with_hit or ranked
    kept: list[float] = []
    bounds: list[tuple[float, float]] = []
    for peak in candidates:
        window = fight_bounds(vod, peak, file_dur)
        if any(not bounds_distinct(window, prev) for prev in bounds):
            continue
        kept.append(float(peak))
        bounds.append(window)
        if len(kept) >= max_clips:
            break
    return kept


__all__ = [
    "bounds_distinct",
    "clip_post_kill_sec",
    "clip_pre_shoot_sec",
    "dedupe_peaks_by_fight_window",
    "fight_bounds",
    "filter_rows_distinct_fights",
    "min_fight_window_gap_sec",
    "peak_blocked_by_used_fights",
    "peak_fight_report",
    "peak_has_kill",
    "peak_shape_ok",
    "select_distinct_kill_peaks",
    "tighten_pubg_clip_bounds",
]
