#!/usr/bin/env python3
"""Expand Genshin highlight peaks back to boss-fight onset (full HP bar)."""

from __future__ import annotations

import logging
import os
from pathlib import Path

log = logging.getLogger("genshin_boss_fight_window")


def _fenv(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except ValueError:
        return default


def _ienv(name: str, default: int) -> int:
    try:
        return int(float(os.environ.get(name, str(default))))
    except ValueError:
        return default


def _bar_at_cap(video_path: Path, t: float, cap) -> float:
    from gameplay_gate import _genshin_boss_bar_score, _read_frame_at

    frame = _read_frame_at(video_path, float(max(0.0, t)), cap)
    if frame is None:
        return 0.0
    return float(_genshin_boss_bar_score(frame))


def expand_boss_fight_window(
    video_path: Path,
    peak_sec: float,
    *,
    vod_duration: float | None = None,
) -> tuple[float, float, dict]:
    """
    From an action peak, walk along the boss HP bar and return (start, duration)
    that begins near fight onset (bar appears / stays high), not mid-fight.
    """
    import cv2

    peak = max(0.0, float(peak_sec))
    step = max(0.5, _fenv("GENSHIN_BOSS_FIGHT_BAR_STEP_SEC", 2.0))
    keep = max(0.02, _fenv("GENSHIN_BOSS_FIGHT_BAR_KEEP", 0.10))
    min_sec = max(8.0, _fenv("GENSHIN_BOSS_FIGHT_MIN_SEC", 28.0))
    max_sec = max(min_sec, _fenv("GENSHIN_BOSS_FIGHT_MAX_SEC", 90.0))
    hard_max = max(max_sec, _fenv("GENSHIN_BOSS_FIGHT_HARD_MAX_SEC", 120.0))
    lead = max(0.0, _fenv("GENSHIN_VOD_LEAD_SEC", 5.0))
    post = max(0.0, _fenv("GENSHIN_BOSS_FIGHT_POST_SEC", 4.0))
    tolerate = max(0, _ienv("GENSHIN_BOSS_FIGHT_GAP_TOLERATE", 3))
    prefer_start = os.environ.get("GENSHIN_BOSS_FIGHT_PREFER_START", "1") == "1"
    enabled = os.environ.get("GENSHIN_BOSS_FULL_FIGHT", "1") == "1"
    if not enabled:
        win = _fenv("HIGHLIGHT_WINDOW_SEC", 15.0)
        start = max(0.0, peak - lead)
        return start, win, {"enabled": False, "onset": start, "peak": peak}

    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        start = max(0.0, peak - lead)
        return start, min_sec, {"enabled": True, "fallback": "no_cap", "onset": start, "peak": peak}

    try:
        if vod_duration is None or vod_duration <= 0:
            fps = float(cap.get(cv2.CAP_PROP_FPS) or 0.0)
            frames = float(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0.0)
            vod_duration = (frames / fps) if fps > 1e-3 else peak + hard_max
        vod_duration = max(peak + 1.0, float(vod_duration))

        # Walk backward while boss bar stays present.
        onset = peak
        t = peak
        miss = 0
        max_back = min(hard_max, peak)
        while (peak - t) < max_back:
            t = max(0.0, t - step)
            bar = _bar_at_cap(video_path, t, cap)
            if bar >= keep:
                onset = t
                miss = 0
            else:
                miss += 1
                if miss > tolerate:
                    break
            if t <= 0.0:
                onset = 0.0
                break

        # Walk forward for fight end (keep climax / finish).
        end = min(vod_duration, peak + post)
        t = peak
        miss = 0
        while (t - onset) < hard_max and t < vod_duration:
            t = min(vod_duration, t + step)
            bar = _bar_at_cap(video_path, t, cap)
            if bar >= keep:
                end = t
                miss = 0
            else:
                miss += 1
                if miss > tolerate:
                    break
    finally:
        cap.release()

    start = max(0.0, onset - lead)
    end = max(start + min_sec, min(vod_duration, end + post))
    dur = end - start

    if dur > max_sec:
        if prefer_start:
            # Keep fight beginning (full HP); truncate the tail.
            end = start + max_sec
            # But never drop the peak if it still fits under hard_max.
            if peak + post - start <= hard_max and peak >= start:
                end = max(end, min(start + hard_max, peak + post))
                if end - start > hard_max:
                    end = start + hard_max
            dur = end - start
        else:
            # Keep climax: pad before peak.
            end = min(vod_duration, peak + post)
            start = max(0.0, end - max_sec)
            dur = end - start

    if dur < min_sec:
        end = min(vod_duration, start + min_sec)
        dur = end - start
        if dur < min_sec and start > 0:
            start = max(0.0, end - min_sec)
            dur = end - start

    meta = {
        "enabled": True,
        "onset": round(onset, 2),
        "peak": round(peak, 2),
        "end": round(end, 2),
        "start": round(start, 2),
        "duration": round(dur, 2),
        "lead": lead,
        "prefer_start": prefer_start,
    }
    log.info(
        "genshin fight window peak=%.1f onset=%.1f start=%.1f dur=%.1f end=%.1f",
        peak,
        onset,
        start,
        dur,
        end,
    )
    return float(start), float(dur), meta
