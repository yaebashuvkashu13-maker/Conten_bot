#!/usr/bin/env python3
"""Genshin VOD fast preflight — sparse boss HP bar HSV probes."""

from __future__ import annotations

import os
from pathlib import Path

from highlight_scorer import normalize_profile


def _probe_offsets(duration: float, *, skip_intro: float) -> list[float]:
    """Dense-enough boss HP probes — sparse 240s steps missed whole fights."""
    dur = max(0.0, float(duration))
    skip = min(float(skip_intro), max(20.0, dur * 0.08))
    if dur < skip + 40:
        return []
    step = float(os.environ.get("GENSHIN_VOD_FAST_PROBE_STEP_SEC", "90"))
    if dur < 600:
        step = min(step, 60.0)
    cap = max(6, int(os.environ.get("GENSHIN_VOD_FAST_PROBE_MAX", "16")))
    offsets: list[float] = []
    t = skip
    while t < dur - 20 and len(offsets) < cap:
        offsets.append(round(t, 1))
        t += step
    return offsets


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

    skip = float(os.environ.get("GENSHIN_VOD_FAST_SKIP_INTRO", "120"))
    offsets = _probe_offsets(dur, skip_intro=skip)
    if not offsets:
        return False, "fast_probe_too_short", []

    bar_min = float(os.environ.get("GENSHIN_VOD_FAST_BAR_MIN", "0.18"))
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
