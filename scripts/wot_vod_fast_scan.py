#!/usr/bin/env python3
"""WoT VOD fast preflight — sparse impact audio probes."""

from __future__ import annotations

import os
from pathlib import Path

from highlight_scorer import WINDOW_SEC, normalize_profile, score_panns_audio


def _probe_offsets(duration: float, *, skip_intro: float) -> list[float]:
    dur = max(0.0, float(duration))
    if dur < skip_intro + 90:
        return []
    offsets: list[float] = []
    for delta in (0, 200, 480, 900, 1400, 2000):
        t = skip_intro + delta
        if t + WINDOW_SEC < dur - 45:
            offsets.append(round(t, 1))
    return sorted(set(offsets))[
        : int(os.environ.get("WOT_VOD_FAST_PROBE_MAX", "6"))
    ]


def vod_fast_impact_check(
    video_path: Path,
    profile: str = "wot",
) -> tuple[bool, str, list[float]]:
    profile = normalize_profile(profile)
    if os.environ.get("WOT_VOD_FAST_PROBE", "1") != "1":
        return True, "fast_probe_disabled", []

    from smart_video_editor import ffprobe_duration

    dur = ffprobe_duration(video_path)
    if dur <= 0:
        return False, "fast_probe_no_duration", []

    skip = float(os.environ.get("WOT_VOD_FAST_SKIP_INTRO", os.environ.get("WOT_FAST_SKIP_INTRO", "45")))
    offsets = _probe_offsets(dur, skip_intro=skip)
    if not offsets:
        return False, "fast_probe_too_short", []

    impact_min = float(os.environ.get("WOT_VOD_FAST_IMPACT_MIN", "0.08"))
    hits: list[float] = []
    top_impact = 0.0
    for t in offsets:
        panns = score_panns_audio(video_path, t, WINDOW_SEC)
        impact = float(panns.get("panns_impact_max", 0) or panns.get("panns_gun_max", 0))
        top_impact = max(top_impact, impact)
        if impact >= impact_min:
            hits.append(t)

    if not hits:
        return False, f"fast_wot_0/{len(offsets)} top_impact={top_impact:.3f}", []
    return True, f"fast_wot_{len(hits)}/{len(offsets)} top_impact={top_impact:.3f}", hits


def apply_fast_probe_seeds(peaks: list[float]) -> None:
    if not peaks or os.environ.get("WOT_VOD_SEED_FROM_FAST_PROBE", "1") != "1":
        return
    os.environ["HIGHLIGHT_ALLOW_SEED_STARTS"] = "1"
    os.environ["HIGHLIGHT_SEED_STARTS"] = ",".join(str(round(p, 1)) for p in peaks[:8])


def clear_fast_probe_seeds() -> None:
    os.environ.pop("HIGHLIGHT_SEED_STARTS", None)
    os.environ.pop("HIGHLIGHT_ALLOW_SEED_STARTS", None)
