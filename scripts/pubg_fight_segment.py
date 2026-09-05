#!/usr/bin/env python3
"""Event-aware PUBG fight bounds: contact -> exchange -> kill/knock -> short tail."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import numpy as np

_CACHE: dict[tuple[str, int, int, int], tuple[float, float, dict[str, Any]]] = {}


def _score_pcm(pcm: np.ndarray) -> tuple[float, float, float]:
    if pcm.size < 384:
        return 0.0, 0.0, 0.0
    samples = pcm.astype(np.float32) / 32768.0
    frame = 256
    energies = [
        float(np.sqrt(np.mean(samples[offset : offset + frame] ** 2)))
        for offset in range(0, len(samples) - frame, frame)
    ]
    if len(energies) < 3:
        return 0.0, 0.0, 0.0
    values = np.asarray(energies, dtype=np.float32)
    median = float(np.median(values))
    peak = float(np.max(values))
    rms = float(np.mean(values))
    floor = max(median * 2.6, 0.010)
    spikes = sum(
        values[index] > floor and values[index] > values[index - 1] * 1.55
        for index in range(1, len(values))
    )
    return spikes / max(len(values) - 1, 1), peak / max(rms, 1e-6), rms


def _activity_timeline(
    video_path: Path,
    scan_start: float,
    scan_end: float,
    *,
    step: float,
    sample: float,
) -> list[dict[str, Any]]:
    from gameplay_gate import _extract_segment_audio_pcm

    sample_rate = 11025
    span = max(sample, scan_end - scan_start + sample)
    pcm = None
    pcm_offset = 0
    try:
        from vod_feature_store import open_store

        store = open_store(video_path, skip_intro=0.0)
        if store is not None and store.ensure_pcm(scan_end + sample):
            full = store.get_pcm_s16()
            if full.size > 0:
                i0 = max(0, int(scan_start * sample_rate))
                i1 = min(len(full), int((scan_start + span) * sample_rate))
                pcm = full[i0:i1]
                pcm_offset = scan_start
            store.close()
    except Exception:
        pcm = None
    if pcm is None:
        from gameplay_gate import _extract_segment_audio_pcm

        pcm = _extract_segment_audio_pcm(
            video_path,
            scan_start,
            span,
            sample_rate=sample_rate,
        )
        pcm_offset = scan_start
    rows: list[dict[str, Any]] = []
    t = scan_start
    while t + sample <= scan_end + 1e-6:
        offset = max(0, int(round((t - pcm_offset) * sample_rate)))
        count = max(1, int(round(sample * sample_rate)))
        gun, burst, rms = _score_pcm(pcm[offset : offset + count])
        score = min(1.0, gun / 0.065) * 0.65
        score += min(1.0, burst / 6.0) * 0.25
        score += min(1.0, rms / 0.050) * 0.10
        rows.append(
            {
                "start": round(t, 2),
                "score": round(min(1.0, score), 4),
                "gun": round(float(gun), 4),
                "burst": round(float(burst), 3),
                "rms": round(float(rms), 4),
            }
        )
        t += step
    return rows


def _gunfire_active_flags(
    timeline: list[dict[str, Any]],
    active_min: float,
) -> list[bool]:
    """Gunfire bins only — motion/ambient must not extend pre-fight lead."""
    gun_min = float(os.environ.get("PUBG_SEGMENT_GUN_ONSET_MIN", "0.025"))
    score_floor = max(active_min, float(os.environ.get("PUBG_SEGMENT_GUN_SCORE_MIN", "0.40")))
    return [
        float(row.get("gun", 0.0)) >= gun_min or float(row.get("score", 0.0)) >= score_floor
        for row in timeline
    ]


def _sustained_onset_index(flags: list[bool], *, streak: int = 2) -> int | None:
    run = 0
    first: int | None = None
    for index, hot in enumerate(flags):
        if hot:
            if first is None:
                first = index
            run += 1
            if run >= streak:
                return first
        else:
            run = 0
            first = None
    return None


def _sustained_gunfire_onset_near_peak(
    timeline: list[dict[str, Any]],
    gun_active: list[bool],
    peak_sec: float,
    *,
    lookback: float = 20.0,
    lookahead: float = 16.0,
    streak: int = 2,
) -> int | None:
    """First sustained gunfire window around peak — skips loot-walk false positives."""
    indices = [
        index
        for index, row in enumerate(timeline)
        if peak_sec - lookback <= float(row["start"]) <= peak_sec + lookahead
    ]
    if not indices:
        return None
    run = 0
    first: int | None = None
    for index in indices:
        if gun_active[index]:
            if first is None:
                first = index
            run += 1
            if run >= streak:
                return first
        else:
            run = 0
            first = None
    hot = [index for index in indices if gun_active[index]]
    return hot[0] if hot else None


def _last_gunfire_index(gun_active: list[bool], *, from_index: int = 0) -> int | None:
    last: int | None = None
    for index in range(max(0, from_index), len(gun_active)):
        if gun_active[index]:
            last = index
    return last


def _fit_window_to_gunfire_span(
    timeline: list[dict[str, Any]],
    gun_active: list[bool],
    *,
    gun_onset: int | None,
    start: float,
    end: float,
    peak_sec: float,
    sample: float,
    contact_lead: float,
    finale_tail: float,
    min_duration: float,
    max_duration: float,
    file_duration: float,
) -> tuple[float, float]:
    """Snap clip to [first sustained shot .. last shot + tail], then cap by max_duration."""
    if gun_onset is None:
        return start, end
    last_gun = _last_gunfire_index(gun_active, from_index=gun_onset)
    if last_gun is None:
        return start, end

    fight_start = max(0.0, float(timeline[gun_onset]["start"]) - contact_lead)
    fight_end = min(
        file_duration,
        float(timeline[last_gun]["start"]) + sample + finale_tail,
    )
    if fight_end - fight_start < min_duration:
        pad = min_duration - (fight_end - fight_start)
        fight_start = max(0.0, fight_start - pad * 0.12)
        fight_end = min(file_duration, fight_end + pad * 0.88)

    start = fight_start
    end = fight_end
    if end - start > max_duration:
        # Keep the full payoff — trim pre-fight lead only.
        start = max(0.0, end - max_duration)
        onset_t = float(timeline[gun_onset]["start"]) - contact_lead
        start = max(start, onset_t)

    start, end = _rebalance_backloaded_window(
        start,
        end,
        peak_sec,
        max_duration=max_duration,
        file_duration=file_duration,
        gun_onset_t=float(timeline[gun_onset]["start"]),
    )
    return start, end


def _rebalance_backloaded_window(
    start: float,
    end: float,
    peak_sec: float,
    *,
    max_duration: float,
    file_duration: float,
    gun_onset_t: float | None = None,
) -> tuple[float, float]:
    """If the fight sits in the last third, slide the window forward."""
    span = end - start
    if span <= 1.0:
        return start, end
    anchor = float(gun_onset_t if gun_onset_t is not None else peak_sec)
    rel = (anchor - start) / span
    trigger = float(os.environ.get("PUBG_SEGMENT_BACKLOAD_REL", "0.45"))
    if rel < trigger:
        return start, end
    target_rel = float(os.environ.get("PUBG_SEGMENT_TARGET_PEAK_REL", "0.28"))
    want_start = anchor - span * target_rel
    start = max(start, want_start)
    if end - start > max_duration:
        start = max(0.0, end - max_duration)
    end = min(file_duration, start + min(span, max_duration))
    return start, end


def _fight_onset_index(
    timeline: list[dict[str, Any]],
    peak_sec: float,
    *,
    active_min: float,
    lookback: float = 18.0,
) -> int | None:
    """First gunfire bin at or before peak — avoids loot-walk left expansion."""
    onset: int | None = None
    for index, row in enumerate(timeline):
        t = float(row["start"])
        if t < peak_sec - lookback:
            continue
        if t > peak_sec + 4.0:
            break
        if float(row["score"]) >= active_min:
            onset = index if onset is None else min(onset, index)
    return onset


def _extend_right_through_payoff(
    timeline: list[dict[str, Any]],
    right: int,
    *,
    peak_sec: float,
    kill_sec: float | None,
    sample: float,
    active_min: float,
) -> int:
    """Kill notifications often fire before sustained gunfire — extend past quiet gaps."""
    min_post = float(os.environ.get("PUBG_SEGMENT_MIN_POST_PEAK_SEC", "16"))
    forward_quiet = max(3, int(os.environ.get("PUBG_SEGMENT_FORWARD_QUIET_BINS", "8")))
    anchor = max(float(peak_sec), float(kill_sec) if kill_sec is not None else float(peak_sec))
    target_end = anchor + min_post
    extended = right
    gun_min = float(os.environ.get("PUBG_SEGMENT_GUN_ONSET_MIN", "0.025"))

    for index, row in enumerate(timeline):
        t = float(row["start"])
        if t < anchor - 2.0:
            continue
        hot = float(row["score"]) >= active_min or float(row.get("gun", 0.0)) >= gun_min
        if hot:
            extended = max(extended, index)
        if t + sample >= target_end:
            break

    quiet = 0
    for index in range(right + 1, len(timeline)):
        t = float(row["start"]) if (row := timeline[index]) else 0.0
        if t > target_end + sample:
            break
        hot = float(row["score"]) >= active_min or float(row.get("gun", 0.0)) >= gun_min
        if hot:
            extended, quiet = index, 0
        else:
            quiet += 1
            if quiet > forward_quiet:
                break
            extended = index
    return extended


def _activity_score(video_path: Path, start: float, duration: float) -> tuple[float, dict]:
    """Compatibility helper for focused tests/tools."""
    from gameplay_gate import score_pubg_gunfire_audio

    gun, burst, rms = score_pubg_gunfire_audio(video_path, start, duration)
    score = min(1.0, gun / 0.065) * 0.65
    score += min(1.0, burst / 6.0) * 0.25
    score += min(1.0, rms / 0.050) * 0.10
    return min(1.0, score), {
        "gun": round(float(gun), 4),
        "burst": round(float(burst), 3),
        "rms": round(float(rms), 4),
    }


def resolve_pubg_fight_bounds(
    video_path: Path,
    peak_sec: float,
    *,
    file_duration: float | None = None,
) -> tuple[float, float, dict[str, Any]]:
    """Resolve adaptive clip start/duration around one detected combat peak."""
    if file_duration is None or float(file_duration) <= 1.0:
        from shooter_vod_segment_feed import _ffprobe_duration

        file_duration = float(_ffprobe_duration(video_path))
    file_duration = float(file_duration)
    stat = video_path.stat()
    key = (str(video_path.resolve()), stat.st_mtime_ns, round(float(peak_sec)), round(file_duration))
    if key in _CACHE:
        return _CACHE[key]

    step = float(os.environ.get("PUBG_SEGMENT_BIN_SEC", "2"))
    sample = float(os.environ.get("PUBG_SEGMENT_SAMPLE_SEC", "3"))
    before = float(os.environ.get("PUBG_SEGMENT_SCAN_BEFORE", "14"))
    after = float(os.environ.get("PUBG_SEGMENT_SCAN_AFTER", "40"))
    active_min = float(os.environ.get("PUBG_SEGMENT_ACTIVITY_MIN", "0.34"))
    max_quiet = max(0, int(os.environ.get("PUBG_SEGMENT_MAX_QUIET_BINS", "2")))
    scan_start = max(0.0, float(peak_sec) - before)
    limit = min(float(file_duration), float(peak_sec) + after)
    timeline = _activity_timeline(
        video_path,
        scan_start,
        limit,
        step=step,
        sample=sample,
    )

    if not timeline:
        start = max(0.0, float(peak_sec) - 7.0)
        result = (start, min(14.0, max(1.0, file_duration - start)), {"fallback": "no_bins"})
        _CACHE[key] = result
        return result

    near = [
        index
        for index, row in enumerate(timeline)
        if abs((float(row["start"]) + sample * 0.5) - peak_sec) <= 7.0
    ]
    seed = max(near or range(len(timeline)), key=lambda index: float(timeline[index]["score"]))
    active = [float(row["score"]) >= active_min for row in timeline]
    active[seed] = True
    gun_active = _gunfire_active_flags(timeline, active_min)
    if any(gun_active[i] for i in (near or [seed])):
        expand_active = gun_active
    elif any(gun_active):
        expand_active = gun_active
    else:
        expand_active = active

    left = seed
    quiet = 0
    for index in range(seed - 1, -1, -1):
        if expand_active[index]:
            left, quiet = index, 0
        else:
            quiet += 1
            if quiet > max_quiet:
                break
            left = index
    right = seed
    quiet = 0
    for index in range(seed + 1, len(timeline)):
        if expand_active[index]:
            right, quiet = index, 0
        else:
            quiet += 1
            if quiet > max_quiet:
                break
            right = index

    contact_lead = float(os.environ.get("PUBG_SEGMENT_CONTACT_LEAD_SEC", "2.0"))
    finale_tail = float(os.environ.get("PUBG_SEGMENT_FINALE_SEC", "3.5"))
    max_preflight = float(os.environ.get("PUBG_SEGMENT_MAX_PREFLIGHT_SEC", "3"))
    gun_onset = _sustained_gunfire_onset_near_peak(
        timeline,
        gun_active,
        peak_sec,
        lookback=before,
        lookahead=min(after, max(20.0, after * 0.9)),
    )
    onset = _fight_onset_index(timeline, peak_sec, active_min=active_min)
    if gun_onset is not None:
        left = max(left, gun_onset)
    elif onset is not None:
        left = max(left, onset)
    start = max(0.0, float(timeline[left]["start"]) - contact_lead)
    if gun_onset is not None:
        start = max(start, float(timeline[gun_onset]["start"]) - contact_lead)
    start = max(start, peak_sec - max_preflight)
    end = min(file_duration, float(timeline[right]["start"]) + sample + finale_tail)

    # Killfeed is sparse: probe only the likely payoff area and extend the finale.
    kill_sec: float | None = None
    kill_score = 0.0
    try:
        from pubg_killfeed_ocr import score_killfeed_segment

        probe_start = max(start, peak_sec - 3.0)
        probe_end = min(file_duration, end + 6.0)
        probe_duration = max(2.0, probe_end - probe_start)
        score, meta = score_killfeed_segment(
            video_path,
            probe_start,
            probe_duration,
            "pubg",
        )
        kill_score = float(score)
        samples = meta.get("notification_samples") or []
        best_sample = max(samples, key=lambda row: float(row.get("score", 0.0)), default=None)
        if best_sample is not None and kill_score >= 0.20:
            frame_count = max(1, int(meta.get("notification_frames") or len(samples) or 1))
            kill_sec = probe_start + (
                float(best_sample.get("index", 0)) + 0.5
            ) * probe_duration / frame_count
        elif kill_score >= 0.20:
            kill_sec = peak_sec
    except Exception:
        pass
    if kill_sec is not None and kill_score >= 0.20:
        end = min(file_duration, max(end, kill_sec + finale_tail))
        start = max(start, peak_sec - max_preflight)
        if kill_sec >= peak_sec - 4.0:
            start = max(0.0, min(start, kill_sec - 6.0))

    right = _extend_right_through_payoff(
        timeline,
        right,
        peak_sec=peak_sec,
        kill_sec=kill_sec,
        sample=sample,
        active_min=active_min,
    )
    end = min(file_duration, max(end, float(timeline[right]["start"]) + sample + finale_tail))

    min_duration = float(os.environ.get("PUBG_SEGMENT_MIN_SEC", "10"))
    max_duration = float(os.environ.get("PUBG_SEGMENT_MAX_SEC", "55"))
    loot_tail_max = float(os.environ.get("PUBG_SEGMENT_LOOT_TAIL_MAX_SEC", "4.0"))
    if kill_sec is not None and timeline:
        post_kill = [
            row
            for row in timeline
            if float(row["start"]) >= float(kill_sec) - 1.0
        ]
        saw_gunfire_after_kill = any(
            float(row.get("gun", 0.0)) >= 0.020 or float(row.get("score", 0.0)) >= active_min
            for row in post_kill
        )
        if saw_gunfire_after_kill:
            quiet_after = 0
            trim_end = end
            seen_gun = False
            for row in post_kill:
                if float(row.get("gun", 0.0)) >= 0.020 or float(row.get("score", 0.0)) >= active_min:
                    seen_gun = True
                    quiet_after = 0
                    continue
                if not seen_gun:
                    continue
                quiet_after += float(os.environ.get("PUBG_SEGMENT_BIN_SEC", "2"))
                if quiet_after >= loot_tail_max:
                    trim_end = min(trim_end, float(row["start"]) + 1.5)
                    break
            end = max(start + min_duration, trim_end)
    if end - start < min_duration:
        need = min_duration - (end - start)
        start = max(0.0, start - need * 0.15)
        end = min(file_duration, end + need * 0.85)
    start, end = _rebalance_backloaded_window(
        start,
        end,
        peak_sec,
        max_duration=max_duration,
        file_duration=file_duration,
        gun_onset_t=float(timeline[gun_onset]["start"]) if gun_onset is not None else None,
    )
    start, end = _fit_window_to_gunfire_span(
        timeline,
        gun_active,
        gun_onset=gun_onset,
        start=start,
        end=end,
        peak_sec=peak_sec,
        sample=sample,
        contact_lead=contact_lead,
        finale_tail=finale_tail,
        min_duration=min_duration,
        max_duration=max_duration,
        file_duration=file_duration,
    )
    if end - start > max_duration:
        preferred_end = end
        if gun_onset is not None:
            onset_t = float(timeline[gun_onset]["start"]) - contact_lead
            start = max(start, onset_t, preferred_end - max_duration)
        else:
            start = max(start, preferred_end - max_duration)
        end = min(file_duration, start + max_duration)
    if peak_sec < start or peak_sec > end:
        end = min(file_duration, max(end, peak_sec + min_duration * 0.65))
        start = max(0.0, end - min_duration)

    # Early-action start shift: if the opening ~2s is quiet, nudge into the fight
    # (+1/+2/+3s) instead of shipping run-up / loot lead-in.
    if os.environ.get("PUBG_EARLY_ACTION_SHIFT", "1") == "1" and timeline:
        try:
            from pubg_combat_timeline import early_action_start_candidates, pick_early_action_start

            window_scores: dict[float, float] = {}
            for cand in early_action_start_candidates(start):
                score = 0.0
                for row in timeline:
                    t = float(row["start"])
                    if cand - 0.05 <= t < cand + 2.0:
                        score = max(
                            score,
                            float(row.get("gun", 0.0) or 0.0),
                            float(row.get("score", 0.0) or 0.0) * 0.55,
                        )
                window_scores[round(cand, 2)] = score
            new_start, _score, _reason = pick_early_action_start(start, window_scores)
            if start < new_start < end - max(4.0, min_duration * 0.45):
                start = float(new_start)
        except Exception:
            pass

    duration = max(1.0, end - start)
    report = {
        "segmenter": "pubg_fight_v1",
        "peak_sec": round(float(peak_sec), 2),
        "contact_start": round(start, 2),
        "contact_sec": round(start, 2),
        "shooting_start": round(
            float(timeline[gun_onset]["start"]) if gun_onset is not None else float(timeline[left]["start"]),
            2,
        ),
        "fight_end": round(end, 2),
        "fight_end_sec": round(end, 2),
        "knock_time": None,
        "kill_time": None if kill_sec is None else round(kill_sec, 2),
        "kill_sec": None if kill_sec is None else round(kill_sec, 2),
        "loot_start": None,
        "killfeed_score": round(kill_score, 3),
        "active_bins": sum(active),
        "total_bins": len(timeline),
        "timeline": timeline,
    }
    result = (round(start, 2), round(duration, 2), report)
    _CACHE[key] = result
    return result


def clear_segment_cache() -> None:
    _CACHE.clear()


__all__ = ["clear_segment_cache", "resolve_pubg_fight_bounds"]
