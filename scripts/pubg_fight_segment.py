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
    file_duration: float,
) -> tuple[float, float, dict[str, Any]]:
    """Resolve adaptive clip start/duration around one detected combat peak."""
    stat = video_path.stat()
    key = (str(video_path.resolve()), stat.st_mtime_ns, round(float(peak_sec)), round(file_duration))
    if key in _CACHE:
        return _CACHE[key]

    step = float(os.environ.get("PUBG_SEGMENT_BIN_SEC", "2"))
    sample = float(os.environ.get("PUBG_SEGMENT_SAMPLE_SEC", "3"))
    before = float(os.environ.get("PUBG_SEGMENT_SCAN_BEFORE", "18"))
    after = float(os.environ.get("PUBG_SEGMENT_SCAN_AFTER", "24"))
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

    left = seed
    quiet = 0
    for index in range(seed - 1, -1, -1):
        if active[index]:
            left, quiet = index, 0
        else:
            quiet += 1
            if quiet > max_quiet:
                break
            left = index
    right = seed
    quiet = 0
    for index in range(seed + 1, len(timeline)):
        if active[index]:
            right, quiet = index, 0
        else:
            quiet += 1
            if quiet > max_quiet:
                break
            right = index

    contact_lead = float(os.environ.get("PUBG_SEGMENT_CONTACT_LEAD_SEC", "2.5"))
    finale_tail = float(os.environ.get("PUBG_SEGMENT_FINALE_SEC", "3.5"))
    start = max(0.0, float(timeline[left]["start"]) - contact_lead)
    end = min(file_duration, float(timeline[right]["start"]) + sample + finale_tail)

    # Killfeed is sparse: probe only the likely payoff area and extend the finale.
    kill_sec = None
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

    min_duration = float(os.environ.get("PUBG_SEGMENT_MIN_SEC", "10"))
    max_duration = float(os.environ.get("PUBG_SEGMENT_MAX_SEC", "28"))
    if end - start < min_duration:
        need = min_duration - (end - start)
        start = max(0.0, start - need * 0.45)
        end = min(file_duration, end + need * 0.55)
    if end - start > max_duration:
        preferred_end = min(end, max(peak_sec + finale_tail, (kill_sec or peak_sec) + finale_tail))
        start = max(start, preferred_end - max_duration)
        end = min(file_duration, start + max_duration)
    if peak_sec < start or peak_sec > end:
        start = max(0.0, peak_sec - min_duration * 0.55)
        end = min(file_duration, start + min_duration)

    duration = max(1.0, end - start)
    report = {
        "segmenter": "pubg_fight_v1",
        "peak_sec": round(float(peak_sec), 2),
        "contact_start": round(start, 2),
        "contact_sec": round(start, 2),
        "shooting_start": round(float(timeline[left]["start"]), 2),
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
