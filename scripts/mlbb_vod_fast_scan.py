#!/usr/bin/env python3
"""MLBB VOD fast preflight — sparse banner color + PANNs combat probes."""

from __future__ import annotations

import os
from pathlib import Path

from highlight_scorer import WINDOW_SEC, normalize_profile, score_panns_audio


def _probe_offsets(duration: float, *, skip_intro: float) -> list[float]:
    dur = max(0.0, float(duration))
    if dur < skip_intro + 120:
        return []
    offsets: list[float] = []
    for delta in (0, 180, 420, 780, 1200, 1800):
        t = skip_intro + delta
        if t + WINDOW_SEC < dur - 60:
            offsets.append(round(t, 1))
    mid = skip_intro + max(0.0, (dur - skip_intro) * 0.45)
    if mid + WINDOW_SEC < dur - 60 and all(abs(mid - x) > 90 for x in offsets):
        offsets.append(round(mid, 1))
    return sorted(set(offsets))[
        : int(os.environ.get("MLBB_VOD_FAST_PROBE_MAX", "6"))
    ]


def _banner_color_at(video_path: Path, t: float) -> float:
    from gameplay_gate import _read_frame_at
    from mlbb_kill_banner import _announce_color_score

    frame = _read_frame_at(video_path, t)
    if frame is None:
        return 0.0
    return float(_announce_color_score(frame))


def vod_fast_combat_check(
    video_path: Path,
    profile: str = "mobile_legends",
) -> tuple[bool, str, list[float]]:
    """
    Six sparse probes: kill-banner color zone + PANNs combat.
    Returns (ok, reason, seed_peak_starts).
    """
    profile = normalize_profile(profile)
    if os.environ.get("MLBB_VOD_FAST_PROBE", "1") != "1":
        return True, "fast_probe_disabled", []

    from smart_video_editor import ffprobe_duration

    dur = ffprobe_duration(video_path)
    if dur <= 0:
        return False, "fast_probe_no_duration", []

    skip = float(os.environ.get("MLBB_VOD_FAST_SKIP_INTRO", "300"))
    offsets = _probe_offsets(dur, skip_intro=skip)
    if not offsets:
        return False, "fast_probe_too_short", []

    color_min = float(os.environ.get("MLBB_VOD_FAST_COLOR_MIN", "0.04"))
    pann_min = float(os.environ.get("MLBB_VOD_FAST_PANN_MIN", "0.12"))
    hits: list[float] = []
    top_color = 0.0
    top_pann = 0.0
    for t in offsets:
        color = _banner_color_at(video_path, t)
        top_color = max(top_color, color)
        panns = score_panns_audio(video_path, t, WINDOW_SEC)
        gmax = float(panns.get("panns_gun_max", 0) or panns.get("panns_combat_max", 0))
        top_pann = max(top_pann, gmax)
        if color >= color_min or gmax >= pann_min:
            hits.append(t)

    if not hits:
        return (
            False,
            f"fast_mlbb_0/{len(offsets)} color={top_color:.3f} pann={top_pann:.3f}",
            [],
        )
    return (
        True,
        f"fast_mlbb_{len(hits)}/{len(offsets)} color={top_color:.3f} pann={top_pann:.3f}",
        hits,
    )


def apply_fast_probe_seeds(peaks: list[float]) -> None:
    if not peaks or os.environ.get("MLBB_VOD_SEED_FROM_FAST_PROBE", "1") != "1":
        return
    os.environ["HIGHLIGHT_ALLOW_SEED_STARTS"] = "1"
    os.environ["HIGHLIGHT_SEED_STARTS"] = ",".join(str(round(p, 1)) for p in peaks[:8])


def clear_fast_probe_seeds() -> None:
    os.environ.pop("HIGHLIGHT_SEED_STARTS", None)
    os.environ.pop("HIGHLIGHT_ALLOW_SEED_STARTS", None)
