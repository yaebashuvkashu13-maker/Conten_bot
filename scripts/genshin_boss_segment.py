#!/usr/bin/env python3
"""Genshin boss segment validation + full-fight expand by HP bar.

Owner rule: ship from the start of the boss fight to the end of the fight
(no mid-fight slice, no cutscene-only pads). Bounds come from the top boss
HP bar — present during combat, gone when the fight ends / cutscene plays.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

log = logging.getLogger("genshin_boss_segment")


def _min_bar_ratio() -> float:
    return float(os.environ.get("GENSHIN_BOSS_BAR_MIN_RATIO", "0.7"))


def _reject_explore_bar() -> float:
    return float(os.environ.get("GENSHIN_BOSS_BAR_REJECT_RATIO", "0.3"))


def boss_bar_ratio_in_segment(
    video_path: Path,
    start_sec: float,
    duration_sec: float,
    *,
    crop_box: tuple[int, int, int, int] | None = None,
) -> tuple[float, float, list[float]]:
    from gameplay_gate import score_genshin_boss_likelihood

    boss_bar, _motion, boss_score, bar_peak = score_genshin_boss_likelihood(
        video_path,
        start_sec,
        duration_sec,
        crop_box=crop_box,
        sample_frames=8,
    )
    return boss_bar, bar_peak, [boss_bar, bar_peak, boss_score]


def validate_genshin_boss_segment(
    video_path: Path,
    start_sec: float,
    duration_sec: float,
    *,
    crop_box: tuple[int, int, int, int] | None = None,
) -> tuple[bool, str, dict]:
    boss_bar, bar_peak, extras = boss_bar_ratio_in_segment(
        video_path, start_sec, duration_sec, crop_box=crop_box
    )
    min_ratio = _min_bar_ratio()
    reject_ratio = _reject_explore_bar()
    metrics = {
        "boss_bar_ratio": round(boss_bar, 4),
        "boss_bar_peak": round(bar_peak, 4),
        "boss_score": round(extras[2], 4) if len(extras) > 2 else 0.0,
    }
    if boss_bar < reject_ratio and bar_peak < reject_ratio * 1.1:
        return False, f"genshin_explore=bar{boss_bar:.3f}", metrics
    if boss_bar < min_ratio * 0.85 and bar_peak < min_ratio:
        return False, f"genshin_no_boss_bar=bar{boss_bar:.3f}:need{min_ratio:.2f}", metrics
    return True, f"genshin_boss_ok=bar{boss_bar:.3f}", metrics


def _bar_at(video_path: Path, t: float, cap=None) -> float:
    from gameplay_gate import _genshin_boss_bar_score, _read_frame_at

    frame = _read_frame_at(video_path, float(t), cap) if cap is not None else _read_frame_at(video_path, float(t))
    if frame is None:
        return 0.0
    return float(_genshin_boss_bar_score(frame))


def expand_genshin_boss_fight(
    video_path: Path,
    peak_sec: float,
    *,
    file_dur: float | None = None,
) -> tuple[float, float, float, dict]:
    """Expand peak → [fight_start, fight_end] using boss HP bar continuity.

    Returns (start, end, duration, meta). Always prefers fight start when
    GENSHIN_BOSS_FIGHT_PREFER_START=1 (default on).
    """
    from gameplay_gate import prefer_ffmpeg_decode
    import cv2

    if file_dur is None or file_dur <= 1.0:
        from smart_video_editor import ffprobe_duration

        file_dur = float(ffprobe_duration(video_path) or 0.0)
    file_dur = max(0.0, float(file_dur))
    peak = float(max(0.0, min(peak_sec, max(0.0, file_dur - 1.0))))

    keep = float(os.environ.get("GENSHIN_BOSS_FIGHT_BAR_KEEP", "0.22"))
    step = max(1.0, float(os.environ.get("GENSHIN_BOSS_FIGHT_BAR_STEP_SEC", "2")))
    # Owner: full fight — allow long back/forward walks (env had 22/40 which truncated).
    max_back = float(os.environ.get("GENSHIN_BOSS_FIGHT_MAX_BACK_SEC", "90"))
    max_fwd = float(os.environ.get("GENSHIN_BOSS_FIGHT_MAX_FORWARD_SEC", "100"))
    min_sec = float(os.environ.get("GENSHIN_BOSS_FIGHT_MIN_SEC", "28"))
    max_sec = float(os.environ.get("GENSHIN_BOSS_FIGHT_MAX_SEC", "100"))
    hard_max = float(os.environ.get("GENSHIN_BOSS_FIGHT_HARD_MAX_SEC", "140"))
    post = float(os.environ.get("GENSHIN_BOSS_FIGHT_POST_SEC", "3"))
    gap_tol = max(1, int(os.environ.get("GENSHIN_BOSS_FIGHT_GAP_TOLERATE", "2")))
    prefer_start = os.environ.get("GENSHIN_BOSS_FIGHT_PREFER_START", "1") == "1"
    full = os.environ.get("GENSHIN_BOSS_FULL_FIGHT", "1") == "1"
    if not full:
        # Legacy short window around peak.
        lead = float(os.environ.get("GENSHIN_VOD_LEAD_SEC", "2"))
        dur = min(max_sec, max(min_sec, 20.0))
        start = max(0.0, peak - lead)
        end = min(file_dur, start + dur) if file_dur > 1 else start + dur
        return start, end, max(1.0, end - start), {"anchor": "peak_window", "peak": peak}

    cap = None
    if not prefer_ffmpeg_decode(video_path):
        cap = cv2.VideoCapture(str(video_path))
        if not cap.isOpened():
            cap = None

    try:
        # Seed: if peak itself has no bar, nudge to nearest hit within ±12s.
        seed = peak
        if _bar_at(video_path, peak, cap) < keep:
            for delta in range(int(step), 13, int(step)):
                for t in (peak - delta, peak + delta):
                    if t < 0 or (file_dur > 1 and t > file_dur - 0.5):
                        continue
                    if _bar_at(video_path, t, cap) >= keep:
                        seed = t
                        break
                if seed != peak:
                    break

        # Walk backward to fight start (first sustained bar presence).
        fight_start = seed
        miss = 0
        t = seed - step
        back_limit = max(0.0, seed - max_back)
        while t >= back_limit:
            bar = _bar_at(video_path, t, cap)
            if bar >= keep:
                fight_start = t
                miss = 0
            else:
                miss += 1
                if miss >= gap_tol:
                    break
            t -= step

        # Walk forward to fight end (bar gone for gap_tol samples).
        fight_end = seed
        miss = 0
        t = seed + step
        fwd_limit = seed + max_fwd
        if file_dur > 1:
            fwd_limit = min(fwd_limit, file_dur - 0.2)
        while t <= fwd_limit:
            bar = _bar_at(video_path, t, cap)
            if bar >= keep:
                fight_end = t
                miss = 0
            else:
                miss += 1
                if miss >= gap_tol:
                    break
            t += step

        fight_end = min(file_dur if file_dur > 1 else fight_end + post, fight_end + post)
        start = max(0.0, fight_start)
        end = max(start + 1.0, fight_end)
        dur = end - start

        # Enforce duration bounds — always keep the start of the fight.
        if dur < min_sec:
            end = min(file_dur if file_dur > 1 else end + (min_sec - dur), start + min_sec)
            dur = end - start
        if dur > hard_max:
            if prefer_start:
                end = start + hard_max
            else:
                # Center on seed but never start after fight_start.
                start = max(fight_start, end - hard_max)
            dur = end - start
        elif dur > max_sec and prefer_start:
            # Soft max: still prefer start→end of fight up to hard_max.
            end = min(end, start + hard_max)
            dur = end - start

        meta = {
            "anchor": "boss_full_fight",
            "peak": round(peak, 2),
            "seed": round(seed, 2),
            "fight_start": round(fight_start, 2),
            "fight_end": round(fight_end, 2),
            "bar_keep": keep,
        }
        log.info(
            "genshin full-fight vod=%s peak=%.1f → [%.1f, %.1f] dur=%.1fs",
            video_path.name,
            peak,
            start,
            end,
            dur,
        )
        return round(start, 2), round(end, 2), round(dur, 2), meta
    finally:
        if cap is not None:
            cap.release()


def apply_genshin_full_fight_clip(
    video_path: Path,
    clip: dict,
    *,
    peak_sec: float | None = None,
) -> dict:
    """Rewrite clip start/duration to full boss fight bounds."""
    peak = float(
        peak_sec
        if peak_sec is not None
        else clip.get("peak_start", clip.get("start", 0)) or 0
    )
    start, end, dur, meta = expand_genshin_boss_fight(video_path, peak)
    out = {
        **clip,
        "start": start,
        "peak_start": peak,
        "fight_end": end,
        "input_duration": dur,
        "output_duration": dur,
        "speed": 1.0,
        "source_path": str(video_path),
        "source_index": 0,
        **meta,
    }
    return out
