#!/usr/bin/env python3
"""Cheap PUBG/Standoff VOD preflight — skip full highlight when no gun audio."""

from __future__ import annotations

import os
from pathlib import Path

from highlight_scorer import WINDOW_SEC, normalize_profile, score_panns_audio


def _owner_good_anchor_starts(video_path: Path, profile: str) -> list[float]:
    if os.environ.get("SHOOTER_VOD_FAST_PROBE_OWNER_BYPASS", "0") != "1":
        return []
    try:
        from vod_owner_learning import owner_labels_for_vod_scan

        rows = owner_labels_for_vod_scan(video_path, profile)
    except Exception:
        return []
    return [
        float(row["time_sec"])
        for row in rows
        if row.get("label") == "good" and "time_sec" in row
    ]


def _probe_offsets(duration: float, *, skip_intro: float) -> list[float]:
    dur = max(0.0, float(duration))
    if dur < skip_intro + 90:
        return []
    offsets: list[float] = []
    for delta in (0, 90, 180, 360, 540, 720, 1200, 1800, 2700, 3600):
        t = skip_intro + delta
        if t + WINDOW_SEC < dur - 45:
            offsets.append(round(t, 1))
    for frac in (0.25, 0.42, 0.58, 0.72):
        t = skip_intro + max(0.0, (dur - skip_intro) * frac)
        if t + WINDOW_SEC < dur - 45 and all(abs(t - x) > 75 for x in offsets):
            offsets.append(round(t, 1))
    return sorted(set(offsets))[: int(os.environ.get("SHOOTER_VOD_FAST_PROBE_MAX", "8"))]


def vod_fast_combat_check(
    video_path: Path,
    profile: str,
) -> tuple[bool, str, list[float]]:
    """
    Sparse PANNs probe (4–8 windows). Returns (ok, reason, gun_peak_starts).
    ~1–3 min vs 15–30 min full highlight on CPU.
    """
    profile = normalize_profile(profile)
    if os.environ.get("SHOOTER_VOD_FAST_PROBE", "1") != "1":
        return True, "fast_probe_disabled", []

    owner_anchors = _owner_good_anchor_starts(video_path, profile)
    if owner_anchors:
        seeds = [
            round(max(0.0, anchor - WINDOW_SEC * 0.5), 1)
            for anchor in owner_anchors[:12]
        ]
        return True, f"fast_probe_owner_bypass labels={len(owner_anchors)}", seeds

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

    gun_min = float(os.environ.get("SHOOTER_VOD_FAST_PANN_MIN", "0.10"))
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
    weak_pass_min = float(os.environ.get("SHOOTER_VOD_FAST_WEAK_PASS_MIN", "0.18"))
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
