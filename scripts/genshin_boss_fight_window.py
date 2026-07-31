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

    Guarantees the peak stays inside the clip. Caps how far back we walk so an
    earlier cutscene / previous fight cannot steal the window.
    """
    import cv2

    peak = max(0.0, float(peak_sec))
    step = max(0.5, _fenv("GENSHIN_BOSS_FIGHT_BAR_STEP_SEC", 2.0))
    keep = max(0.02, _fenv("GENSHIN_BOSS_FIGHT_BAR_KEEP", 0.10))
    min_sec = max(8.0, _fenv("GENSHIN_BOSS_FIGHT_MIN_SEC", 28.0))
    max_sec = max(min_sec, _fenv("GENSHIN_BOSS_FIGHT_MAX_SEC", 100.0))
    hard_max = max(max_sec, _fenv("GENSHIN_BOSS_FIGHT_HARD_MAX_SEC", 140.0))
    lead = max(0.0, _fenv("GENSHIN_VOD_LEAD_SEC", 3.0))
    post = max(0.0, _fenv("GENSHIN_BOSS_FIGHT_POST_SEC", 10.0))
    # How far before the peak we may walk (blocks cutscene / prior fight).
    max_back = max(12.0, _fenv("GENSHIN_BOSS_FIGHT_MAX_BACK_SEC", 45.0))
    # How far after the peak we may walk (blocks post-fight UI false bars).
    max_forward = max(8.0, _fenv("GENSHIN_BOSS_FIGHT_MAX_FORWARD_SEC", 40.0))
    tolerate = max(0, _ienv("GENSHIN_BOSS_FIGHT_GAP_TOLERATE", 3))
    prefer_start = os.environ.get("GENSHIN_BOSS_FIGHT_PREFER_START", "0") == "1"
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

        # Walk backward while boss bar stays present — but not endlessly.
        onset = peak
        t = peak
        miss = 0
        max_back_span = min(hard_max, max_back, peak)
        while (peak - t) < max_back_span:
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
                break

        # Never keep an onset older than max_back before the peak.
        onset = max(onset, peak - max_back)

        # Walk forward for fight end — capped so false bars don't stretch forever.
        end = min(vod_duration, peak + post)
        t = peak
        miss = 0
        while (t - peak) < max_forward and (t - onset) < hard_max and t < vod_duration:
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
    # Peak must always sit inside the clip with post-roll room.
    end = max(end, min(vod_duration, peak + post))
    if peak < start:
        start = max(0.0, peak - lead)
    dur = end - start

    if dur > max_sec:
        if prefer_start:
            # Keep fight opening, but never drop the peak / finish.
            need_end = min(vod_duration, peak + post)
            end = max(start + max_sec, need_end)
            if end - start > max_sec:
                start = max(0.0, end - max_sec)
            if peak < start:
                start = max(0.0, peak - min(lead + 8.0, max_back * 0.5))
                end = min(vod_duration, max(start + min_sec, peak + post))
            if end - start > hard_max:
                end = min(vod_duration, peak + post)
                start = max(0.0, end - hard_max)
            dur = end - start
        else:
            # Default: keep climax + finish; trim early tail (cutscenes go first).
            end = min(vod_duration, max(end, peak + post))
            start = max(0.0, end - max_sec)
            # Prefer not cutting off more pre-peak combat than max_back allows.
            earliest = max(0.0, peak - max_back - lead)
            if start < earliest:
                start = earliest
                end = min(vod_duration, start + max_sec)
                end = max(end, min(vod_duration, peak + post))
                if end - start > max_sec:
                    start = max(0.0, end - max_sec)
            if peak < start:
                start = max(0.0, peak - lead)
                end = min(vod_duration, max(start + min_sec, peak + post))
            if end - start > hard_max:
                end = min(vod_duration, peak + post)
                start = max(0.0, end - hard_max)
            dur = end - start

    if dur < min_sec:
        end = min(vod_duration, start + min_sec)
        dur = end - start
        if dur < min_sec and start > 0:
            start = max(0.0, end - min_sec)
            dur = end - start

    # Final safety: peak inside window.
    if not (start - 0.05 <= peak <= end + 0.05):
        end = min(vod_duration, max(end, peak + post))
        start = max(0.0, min(start, peak - lead))
        if end - start > hard_max:
            start = max(0.0, end - hard_max)
        dur = end - start

    meta = {
        "enabled": True,
        "onset": round(onset, 2),
        "peak": round(peak, 2),
        "end": round(end, 2),
        "start": round(start, 2),
        "duration": round(dur, 2),
        "lead": lead,
        "post": post,
        "max_back": max_back,
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
