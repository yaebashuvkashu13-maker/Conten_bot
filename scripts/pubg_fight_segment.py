#!/usr/bin/env python3
"""Event-aware PUBG fight bounds: contact -> exchange -> kill/knock -> short tail."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

_CACHE: dict[tuple[str, int, int, int], tuple[float, float, dict[str, Any]]] = {}


def _activity_score(video_path: Path, start: float, duration: float) -> tuple[float, dict]:
    from gameplay_gate import score_pubg_gunfire_audio
    from highlight_scorer import score_panns_audio

    gun, burst, rms = score_pubg_gunfire_audio(video_path, start, duration)
    panns = score_panns_audio(video_path, start, duration)
    pmax = float(panns.get("panns_gun_max", 0.0))
    score = min(1.0, gun / 0.065) * 0.50
    score += min(1.0, burst / 6.0) * 0.20
    score += min(1.0, pmax / 0.35) * 0.30
    return min(1.0, score), {
        "gun": round(float(gun), 4),
        "burst": round(float(burst), 3),
        "rms": round(float(rms), 4),
        "panns": round(pmax, 4),
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
    timeline: list[dict[str, Any]] = []
    t = max(0.0, float(peak_sec) - before)
    limit = min(float(file_duration), float(peak_sec) + after)
    while t + sample <= limit + 1e-6:
        score, metrics = _activity_score(video_path, t, sample)
        timeline.append({"start": round(t, 2), "score": round(score, 4), **metrics})
        t += step

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

        probe = max(start, peak_sec - 3.0)
        while probe < min(file_duration - 2.0, end + 6.0):
            score, _ = score_killfeed_segment(video_path, probe, 4.0, "pubg")
            if score > kill_score:
                kill_score = float(score)
                kill_sec = probe + 2.0
            probe += 4.0
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
        "contact_sec": round(start, 2),
        "fight_end_sec": round(end, 2),
        "kill_sec": None if kill_sec is None else round(kill_sec, 2),
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
