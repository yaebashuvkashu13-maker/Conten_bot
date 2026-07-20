#!/usr/bin/env python3
"""Genshin VOD fast preflight — sparse boss HP bar HSV probes."""

from __future__ import annotations

import os
from pathlib import Path

from highlight_scorer import normalize_profile


def _probe_offsets(duration: float, *, skip_intro: float) -> list[float]:
    """Build probe times that work for short boss VODs (~2–6 min) and long streams."""
    dur = max(0.0, float(duration))
    fight_min = float(os.environ.get("GENSHIN_BOSS_FIGHT_MIN_SEC", "28"))
    # Need room for at least one fight window after intro.
    min_needed = skip_intro + max(45.0, fight_min)
    if dur < min_needed:
        # Very short clip: still probe the middle if it can hold a fight.
        if dur >= fight_min + 10:
            mid = max(5.0, dur * 0.35)
            return [round(mid, 1)]
        return []

    # Adaptive steps — denser on short/medium boss VODs, sparse on long streams.
    if dur <= 360:
        deltas = (0, 30, 60, 90, 120, 180, 240)
    elif dur <= 900:
        deltas = (0, 60, 120, 240, 360, 540, 720)
    else:
        deltas = (0, 240, 600, 960, 1500, 2100)

    offsets: list[float] = []
    for delta in deltas:
        t = skip_intro + delta
        if t < dur - 20:
            offsets.append(round(t, 1))
    return sorted(set(offsets))[
        : int(os.environ.get("GENSHIN_VOD_FAST_PROBE_MAX", "8"))
    ]


def _boss_bar_at(video_path: Path, t: float) -> float:
    from gameplay_gate import _genshin_boss_bar_score, _read_frame_at

    frame = _read_frame_at(video_path, t)
    if frame is None:
        return 0.0
    return float(_genshin_boss_bar_score(frame))


def vod_fast_boss_check(
    video_path: Path,
    profile: str = "genshin",
) -> tuple[bool, str, list[float]]:
    profile = normalize_profile(profile)
    if os.environ.get("GENSHIN_VOD_FAST_PROBE", "1") != "1":
        return True, "fast_probe_disabled", []

    from smart_video_editor import ffprobe_duration

    dur = ffprobe_duration(video_path)
    if dur <= 0:
        return False, "fast_probe_no_duration", []

    # Boss highlight VODs are often 3–8 min — don't require 120s intro + 90s tail.
    skip = float(os.environ.get("GENSHIN_VOD_FAST_SKIP_INTRO", "45"))
    if dur < 360:
        skip = min(skip, float(os.environ.get("GENSHIN_VOD_FAST_SKIP_INTRO_SHORT", "20")))
    offsets = _probe_offsets(dur, skip_intro=skip)
    if not offsets:
        return False, f"fast_probe_too_short=dur{dur:.0f}", []

    bar_min = float(os.environ.get("GENSHIN_VOD_FAST_BAR_MIN", "0.14"))
    hits: list[float] = []
    top_bar = 0.0
    for t in offsets:
        bar = _boss_bar_at(video_path, t)
        top_bar = max(top_bar, bar)
        if bar >= bar_min:
            hits.append(t)

    if not hits:
        return False, f"fast_genshin_0/{len(offsets)} top_bar={top_bar:.3f}", []
    return True, f"fast_genshin_{len(hits)}/{len(offsets)} top_bar={top_bar:.3f}", hits


def apply_fast_probe_seeds(peaks: list[float]) -> None:
    if not peaks or os.environ.get("GENSHIN_VOD_SEED_FROM_FAST_PROBE", "1") != "1":
        return
    os.environ["HIGHLIGHT_ALLOW_SEED_STARTS"] = "1"
    os.environ["HIGHLIGHT_SEED_STARTS"] = ",".join(str(round(p, 1)) for p in peaks[:8])


def clear_fast_probe_seeds() -> None:
    os.environ.pop("HIGHLIGHT_SEED_STARTS", None)
    os.environ.pop("HIGHLIGHT_ALLOW_SEED_STARTS", None)
