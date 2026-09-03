#!/usr/bin/env python3
"""Shared PUBG montage fight-window logic — main feed + owner redo."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

log = logging.getLogger("pubg_montage_bounds")


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


def assemble_gun_pad_sec() -> float:
    """Padding before/after sustained gunfire when owner assembles 👍 singles."""
    return float(os.environ.get("PUBG_ASSEMBLE_GUN_PAD_SEC", "4.0"))


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


def _ensure_payoff_in_clip(
    start: float,
    dur: float,
    peak: float,
    report: dict[str, Any],
) -> tuple[float, float]:
    """Expand/shift clip so kill notification stays inside after tighten."""
    kill = report.get("kill_sec") if report.get("kill_sec") is not None else report.get("kill_time")
    post = clip_post_kill_sec()
    end = start + dur
    if kill is not None:
        k = float(kill)
        if k + post > end:
            end = k + post
        if k < start:
            start = max(0.0, k - clip_pre_shoot_sec())
        dur = max(8.0, end - start)
    return float(start), float(dur)


def _gun_bin_active(row: dict[str, Any]) -> bool:
    gun_min = float(os.environ.get("PUBG_SEGMENT_GUN_ONSET_MIN", "0.025"))
    active_min = float(os.environ.get("PUBG_SEGMENT_ACTIVITY_MIN", "0.34"))
    try:
        return float(row.get("gun", 0.0) or 0.0) >= gun_min or float(row.get("score", 0.0) or 0.0) >= active_min
    except (TypeError, ValueError):
        return False


def clip_ends_on_gunfire(
    start: float,
    dur: float,
    report: dict[str, Any],
    *,
    tail_sec: float | None = None,
) -> bool:
    """True when the last seconds of the clip are still mid-burst."""
    timeline = report.get("timeline")
    if not isinstance(timeline, list) or not timeline:
        return False
    tail = float(tail_sec if tail_sec is not None else os.environ.get("PUBG_CLIP_END_GUN_TAIL_SEC", "2.5"))
    end = float(start) + float(dur)
    tail_start = end - tail
    for row in timeline:
        try:
            t = float(row["start"])
        except (KeyError, TypeError, ValueError):
            continue
        if t < tail_start - 0.5 or t > end + 1.0:
            continue
        if _gun_bin_active(row):
            return True
    return False


def extend_end_past_active_gunfire(
    start: float,
    dur: float,
    report: dict[str, Any],
    *,
    max_dur: float,
    single: bool = False,
) -> tuple[float, float]:
    """Do not cut while the player is still shooting — extend to quiet (or max)."""
    timeline = report.get("timeline")
    if not isinstance(timeline, list) or not timeline:
        return float(start), float(dur)
    if not clip_ends_on_gunfire(start, dur, report):
        return float(start), float(dur)

    bin_sec = float(os.environ.get("PUBG_SEGMENT_BIN_SEC", "2"))
    quiet_need = float(os.environ.get("PUBG_CLIP_END_QUIET_SEC", "2.5"))
    start_f = float(start)
    end = start_f + float(dur)
    hard_cap = start_f + max(8.0, float(max_dur))
    fight_end = report.get("fight_end") or report.get("fight_end_sec")
    if fight_end is not None:
        hard_cap = min(hard_cap, float(fight_end) + quiet_need)

    rows = sorted(timeline, key=lambda r: float(r.get("start") or 0))
    new_end = end
    quiet = 0.0
    for row in rows:
        try:
            t = float(row["start"])
        except (KeyError, TypeError, ValueError):
            continue
        if t < end - bin_sec:
            continue
        if t > hard_cap:
            break
        if _gun_bin_active(row):
            quiet = 0.0
            new_end = max(new_end, min(hard_cap, t + bin_sec))
            continue
        if t >= end - bin_sec:
            quiet += bin_sec
            new_end = max(new_end, min(hard_cap, t + 0.5))
            if quiet >= quiet_need:
                break

    new_dur = max(float(dur), new_end - start_f)
    # If still ends hot at hard cap, shift window forward to land on quiet when possible.
    if clip_ends_on_gunfire(start_f, new_dur, report) and single:
        quiet_t = None
        quiet = 0.0
        for row in rows:
            try:
                t = float(row["start"])
            except (KeyError, TypeError, ValueError):
                continue
            if t < start_f:
                continue
            if _gun_bin_active(row):
                quiet = 0.0
                continue
            quiet += bin_sec
            if quiet >= quiet_need:
                quiet_t = t + 0.5
                break
        if quiet_t is not None and quiet_t > start_f + 8.0:
            new_end = min(hard_cap, quiet_t)
            new_start = max(0.0, new_end - float(max_dur))
            shoot = report.get("shooting_start")
            if shoot is not None:
                new_start = min(new_start, float(shoot))
            return float(new_start), float(max(8.0, new_end - new_start))
    return float(start_f), float(new_dur)


def tighten_pubg_clip_bounds(
    start: float,
    dur: float,
    report: dict[str, Any],
    *,
    peak: float | None = None,
    single: bool = False,
) -> tuple[float, float]:
    """Start at gunfire, end soon after kill — no loot-walk tail.

    Never end mid-burst: if the tail is still gunfire, extend through the fight.
    """
    from pubg_clip_shape_gate import (
        aggressive_tighten_for_shape,
        max_peak_position_frac,
        validate_clip_fight_shape,
    )

    pre_pad = clip_pre_shoot_sec()
    post_kill = clip_post_kill_sec()
    max_lead = float(os.environ.get("PUBG_CLIP_MAX_PRE_SHOOT_SEC", "1.2"))
    min_dur = float(os.environ.get("PUBG_CLIP_MIN_TIGHTEN_SEC", "18"))
    if single:
        min_dur = max(min_dur, float(os.environ.get("PUBG_SINGLE_MIN_SEC", "20")))
    max_dur = float(
        os.environ.get("PUBG_SINGLE_MAX_SEC", "90")
        if single
        else os.environ.get("PUBG_SEGMENT_MAX_SEC", "55")
    )

    shoot = report.get("shooting_start")
    if shoot is not None:
        start = float(shoot) - min(pre_pad, max_lead)
    kill = report.get("kill_sec") if report.get("kill_sec") is not None else report.get("kill_time")
    fight_end = report.get("fight_end") or report.get("fight_end_sec")
    end = float(start) + float(dur)

    # Montage: prefer end soon after kill unless gunfire continues hard after kill.
    gun_continues_after_kill = False
    if kill is not None and isinstance(report.get("timeline"), list):
        for row in report["timeline"]:
            try:
                t = float(row["start"])
            except (KeyError, TypeError, ValueError):
                continue
            if t < float(kill) + 1.0:
                continue
            if t > float(kill) + 8.0:
                break
            if _gun_bin_active(row):
                gun_continues_after_kill = True
                break

    if kill is not None and not single and not gun_continues_after_kill:
        end = min(end, float(kill) + post_kill)
    if fight_end is not None:
        end = min(end, float(fight_end))
    raw_dur = max(0.0, end - float(start))
    kill_end = float(kill) + post_kill if kill is not None else None
    long_loot_tail = (
        kill is not None
        and not single
        and not gun_continues_after_kill
        and fight_end is not None
        and kill_end is not None
        and float(fight_end) > kill_end + 2.0
    )
    if long_loot_tail and kill_end is not None:
        dur = min(raw_dur, max(0.0, kill_end - float(start)))
    else:
        dur = max(min_dur, raw_dur)

    if peak is not None:
        ok, reason = validate_clip_fight_shape(start, dur, float(peak), report)
        if not ok and "fight_at_end" in reason and shoot is not None:
            max_frac = max_peak_position_frac()
            need_span = (float(peak) - float(shoot) + max_lead) / max(max_frac, 0.05)
            start = max(0.0, float(shoot) - max_lead)
            if long_loot_tail and kill_end is not None:
                end = float(kill_end)
                dur = max(0.0, end - start)
            else:
                end = max(end, start + max(min_dur, need_span))
                if fight_end is not None:
                    end = min(end, float(fight_end))
                dur = max(min_dur, end - start)
            ok, reason = validate_clip_fight_shape(start, dur, float(peak), report)
        if not ok:
            alt_start, alt_dur = aggressive_tighten_for_shape(
                start, dur, float(peak), report, single=single
            )
            if long_loot_tail and kill_end is not None:
                alt_end = min(alt_start + alt_dur, float(kill_end))
                alt_dur = max(0.0, alt_end - alt_start)
            min_alt = 0.0 if long_loot_tail else min_dur
            if alt_dur >= min_alt:
                alt_ok, _alt_reason = validate_clip_fight_shape(
                    alt_start, alt_dur, float(peak), report
                )
                if alt_ok:
                    start, dur = alt_start, alt_dur
                    ok = True
        if ok:
            start, dur = _ensure_payoff_in_clip(start, dur, float(peak), report)
            if not long_loot_tail:
                dur = max(min_dur, dur)

    start, dur = extend_end_past_active_gunfire(
        start, dur, report, max_dur=max_dur, single=single
    )
    dur = min(float(dur), float(max_dur))
    return float(start), float(dur)


def _gunfire_end_from_report(report: dict[str, Any], *, fallback: float) -> float:
    timeline = report.get("timeline") or []
    gun_times: list[float] = []
    for row in timeline:
        try:
            if float(row.get("gun", 0.0)) >= 0.020 or float(row.get("score", 0.0)) >= 0.020:
                gun_times.append(float(row["start"]))
        except (TypeError, ValueError, KeyError):
            continue
    if gun_times:
        sample = float(os.environ.get("PUBG_SEGMENT_BIN_SEC", "2"))
        return max(gun_times) + sample
    for key in ("fight_end", "fight_end_sec"):
        if report.get(key) is not None:
            return float(report[key])
    return float(fallback)


def tighten_pubg_assemble_bounds(
    start: float,
    dur: float,
    report: dict[str, Any],
    *,
    peak: float,
    file_dur: float,
) -> tuple[float, float]:
    """Re-trim 👍 singles for montage: drop loot-walk, keep gunfire ± pad sec."""
    from pubg_clip_shape_gate import aggressive_tighten_for_shape, validate_clip_fight_shape

    pad = assemble_gun_pad_sec()
    min_dur = max(8.0, float(os.environ.get("PUBG_ASSEMBLE_MIN_SEC", "10")))
    shoot = report.get("shooting_start")
    core_start = float(shoot) if shoot is not None else float(peak)
    core_end = _gunfire_end_from_report(report, fallback=float(start) + float(dur))
    start = max(0.0, core_start - pad)
    end = min(float(file_dur), core_end + pad)
    dur = max(min_dur, end - start)

    ok, reason = validate_clip_fight_shape(start, dur, float(peak), report)
    if not ok:
        alt_start, alt_dur = aggressive_tighten_for_shape(start, dur, float(peak), report)
        if alt_dur >= min_dur:
            alt_ok, _ = validate_clip_fight_shape(alt_start, alt_dur, float(peak), report)
            if alt_ok:
                start, dur = alt_start, alt_dur
                ok = True
        if not ok:
            log.warning("assemble tighten shape reject peak=%.1f: %s", peak, reason)
    return float(start), float(dur)


def pubg_clip_has_gunfire(
    vod: Path,
    start: float,
    dur: float,
    peak: float,
    *,
    single: bool = False,
) -> tuple[bool, str]:
    """Reject running/menu clips — require audible gunfire in the fight core."""
    from gameplay_gate import score_pubg_gunfire_audio

    min_gun = float(os.environ.get("PUBG_CLIP_MIN_GUN_DENSITY", "0.055"))
    if single:
        min_gun = float(os.environ.get("PUBG_SINGLE_MIN_GUN_DENSITY", str(min_gun)))
    min_burst = float(os.environ.get("PUBG_CLIP_MIN_BURST_RATIO", "4.8"))

    gun, burst, _rms = score_pubg_gunfire_audio(vod, start, dur)
    if gun >= min_gun and burst >= min_burst:
        return True, "gun_ok"

    core_start = max(start, float(peak) - 10.0)
    core_dur = min(float(dur), max(8.0, float(peak) - core_start + 8.0))
    gun_core, burst_core, _ = score_pubg_gunfire_audio(vod, core_start, core_dur)
    if gun_core >= min_gun and burst_core >= min_burst:
        return True, "gun_core_ok"

    return False, f"low_gun whole={gun:.3f} core={gun_core:.3f} burst={burst:.2f} need>={min_gun:.3f}/{min_burst:.1f}"


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


def peak_presend_ok(
    vod: Path,
    peak: float,
    *,
    file_dur: float | None = None,
) -> tuple[bool, str, float, float]:
    """Full presend check on tightened fight clip — not just shape."""
    from pubg_fight_segment import resolve_pubg_fight_bounds
    from pubg_quality_score import score_pubg_window

    if file_dur is None:
        from shooter_vod_segment_feed import _ffprobe_duration

        file_dur = _ffprobe_duration(vod)
    start, dur, report = resolve_pubg_fight_bounds(vod, peak, file_duration=file_dur)
    start, dur = tighten_pubg_clip_bounds(start, dur, report, peak=float(peak))
    dur = max(10.0, float(dur))
    ok, reason, _rep = score_pubg_window(vod, start, dur)
    return ok, reason, float(start), float(dur)


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
    ranked, _reason, meta = rank_peaks_fast(
        vod,
        filtered,
        "pubg",
        part_sec=14.0,
        max_probes=max(len(filtered), min_clips * 8),
    )
    presend_hits: list[tuple[float, float]] = []
    for p in ranked:
        ok, _reason, _start, _dur = peak_presend_ok(vod, float(p), file_dur=file_dur)
        if ok:
            note = float(meta.get(float(p), {}).get("notification_score", 0.0) or 0.0)
            presend_hits.append((float(p), note))
    presend_hits.sort(key=lambda item: -item[1])
    with_hit = [p for p, _note in presend_hits]
    if len(with_hit) < min_clips:
        with_hit = [
            float(p)
            for p in ranked
            if meta.get(float(p), {}).get("notification_hit")
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
    "peak_presend_ok",
    "peak_shape_ok",
    "select_distinct_kill_peaks",
    "tighten_pubg_clip_bounds",
]
