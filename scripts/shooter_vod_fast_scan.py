#!/usr/bin/env python3
"""Cheap PUBG/Standoff VOD preflight — skip full highlight when no gun audio.

Operational scene hunt (fast → slow):
1. Sparse PANNs gun probes across the VOD (this module) — seconds–~2 min.
2. Seed stage1 from gun hits; expand neighborhoods (seed-fast) — skip cold
   full-VOD analyze_video when possible.
3. Score only prefiltered combat windows; stop once montage has enough peaks.
4. Never encode a clip before a cheap combat/metro window check.

Full motion analyze is a last resort, not the default map walk.
"""

from __future__ import annotations

import os
from pathlib import Path

from highlight_scorer import WINDOW_SEC, normalize_profile, score_panns_audio


def _probe_offsets(duration: float, *, skip_intro: float) -> list[float]:
    """Gun-hunt offsets: denser on short/mid VODs, sparse on long ones."""
    dur = max(0.0, float(duration))
    if dur < 90:
        return []

    skip = float(skip_intro)
    # Short VODs still have fights — don't demand skip+90 headroom.
    if dur < float(os.environ.get("SHOOTER_VOD_FAST_SHORT_DUR_SEC", "900")):
        skip = min(
            skip,
            float(os.environ.get("SHOOTER_VOD_FAST_SKIP_INTRO_SHORT", "60")),
        )

    max_n = max(3, int(os.environ.get("SHOOTER_VOD_FAST_PROBE_MAX", "8")))
    tail = 20.0 if dur < 600 else 45.0
    offsets: list[float] = []

    if dur < float(os.environ.get("SHOOTER_VOD_FAST_SHORT_DUR_SEC", "900")):
        step = float(os.environ.get("SHOOTER_VOD_FAST_PROBE_STEP_SHORT", "90"))
        t = skip
        while t + WINDOW_SEC < dur - tail and len(offsets) < max_n:
            offsets.append(round(t, 1))
            t += step
        return sorted(set(offsets))

    for delta in (0, 90, 180, 300, 480, 720, 960, 1200, 1500, 1800):
        t = skip + delta
        if t + WINDOW_SEC < dur - tail:
            offsets.append(round(t, 1))
    mid = skip + max(0.0, (dur - skip) * 0.42)
    if mid + WINDOW_SEC < dur - tail and all(abs(mid - x) > 60 for x in offsets):
        offsets.append(round(mid, 1))
    return sorted(set(offsets))[:max_n]


def vod_fast_combat_check(
    video_path: Path,
    profile: str,
) -> tuple[bool, str, list[float]]:
    """
    Sparse PANNs probe. Returns (ok, reason, gun_peak_starts).
    ~1–3 min vs 15–30 min full highlight on CPU.
    """
    profile = normalize_profile(profile)
    if os.environ.get("SHOOTER_VOD_FAST_PROBE", "1") != "1":
        return True, "fast_probe_disabled", []

    from smart_video_editor import ffprobe_duration

    dur = ffprobe_duration(video_path)
    if dur <= 0:
        return False, "fast_probe_no_duration", []

    skip = float(
        os.environ.get(
            "PUBG_METRO_VOD_SKIP_INTRO_SEC",
            os.environ.get("SHOOTER_VOD_FAST_SKIP_INTRO", "120"),
        )
    )
    offsets = _probe_offsets(dur, skip_intro=skip)
    if not offsets:
        return False, "fast_probe_too_short", []

    gun_min = float(os.environ.get("SHOOTER_VOD_FAST_PANN_MIN", "0.08"))
    hits: list[float] = []
    top_gun = 0.0
    for t in offsets:
        panns = score_panns_audio(video_path, t, WINDOW_SEC)
        gmax = float(panns.get("panns_gun_max", 0))
        top_gun = max(top_gun, gmax)
        if gmax >= gun_min:
            hits.append(t)

    if not hits:
        return (
            False,
            f"fast_panns_0/{len(offsets)} top={top_gun:.3f} min={gun_min:.2f}",
            [],
        )

    min_hits = max(1, int(os.environ.get("SHOOTER_VOD_FAST_MIN_HITS", "1")))
    strong_min = float(os.environ.get("SHOOTER_VOD_FAST_STRONG_PANN", "0.40"))
    weak_pass_min = float(os.environ.get("SHOOTER_VOD_FAST_WEAK_PASS_MIN", "0.14"))
    if len(hits) < min_hits:
        if len(hits) == 1 and top_gun >= strong_min:
            return (
                True,
                f"fast_panns_strong_1/{len(offsets)} top={top_gun:.3f}",
                hits,
            )
        if top_gun >= weak_pass_min:
            return (
                True,
                f"fast_panns_weak_{len(hits)}/{len(offsets)} top={top_gun:.3f} min_hits={min_hits}",
                hits,
            )
        return (
            False,
            f"fast_panns_{len(hits)}/{len(offsets)} top={top_gun:.3f} min_hits={min_hits}",
            hits,
        )
    return True, f"fast_panns_{len(hits)}/{len(offsets)} top={top_gun:.3f}", hits


def apply_fast_probe_seeds(peaks: list[float]) -> None:
    if not peaks or os.environ.get("SHOOTER_VOD_SEED_FROM_FAST_PROBE", "1") != "1":
        return
    os.environ["HIGHLIGHT_ALLOW_SEED_STARTS"] = "1"
    os.environ["HIGHLIGHT_SEED_STARTS"] = ",".join(str(round(p, 1)) for p in peaks[:8])


def clear_fast_probe_seeds() -> None:
    os.environ.pop("HIGHLIGHT_SEED_STARTS", None)
    os.environ.pop("HIGHLIGHT_ALLOW_SEED_STARTS", None)
