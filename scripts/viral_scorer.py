#!/usr/bin/env python3
"""Viral gaming highlight ranker — hook-first, payoff timing, menu penalty."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import numpy as np

HOOK_MENU_MAX = float(os.environ.get("VIRAL_HOOK_MENU_MAX", "0.35"))
HOOK_MIN_SCORE = float(os.environ.get("VIRAL_HOOK_MIN", "0.42"))
SEGMENT_HOOK_MIN = float(os.environ.get("VIRAL_SEGMENT_HOOK_MIN", "0.35"))


def hook_score_frame(frame, profile: str) -> tuple[float, dict[str, float]]:
    """First-frame hook: action in motion, NOT menu/lobby."""
    from visual_action_check import check_frame_visual, _frame_hit_flash_score, _frame_menu_overlay
    from visual_action_check import _laplacian_edge_score

    profile = profile.strip().lower()
    if profile == "mlbb":
        profile = "mobile_legends"
    if profile == "world_of_tanks":
        profile = "wot"

    menu = _frame_menu_overlay(frame)
    center_edge = _laplacian_edge_score(frame, 0.22, 0.72, 0.12, 0.88)
    flash = _frame_hit_flash_score(frame)
    ok, _, vis = check_frame_visual(profile, frame)

    menu_penalty = min(1.0, menu / max(HOOK_MENU_MAX, 0.01))
    action = min(1.0, center_edge * 8.0 + flash * 40.0)
    score = action * (1.0 - menu_penalty * 0.85)
    if ok:
        score = min(1.0, score + 0.12)
    else:
        score *= 0.55

    return round(max(0.0, min(1.0, score)), 4), {
        "menu_overlay": round(menu, 4),
        "center_edge": round(center_edge, 4),
        "hit_flash": round(flash, 4),
        "visual_ok": float(ok),
    }


def hook_score(video_path: Path, start_sec: float, profile: str) -> tuple[float, dict[str, float]]:
    from gameplay_gate import _read_frame_at, detect_game_viewport_crop

    t = start_sec + 0.15
    frame = _read_frame_at(video_path, t)
    if frame is None:
        return 0.0, {"reason": "frame_missing"}
    crop = detect_game_viewport_crop(video_path, start_sec, 10.0)
    if crop is not None:
        x, y, w, h = crop
        frame = frame[y : y + h, x : x + w]
    return hook_score_frame(frame, profile)


def payoff_timing_score(metrics: Any, duration_sec: float = 10.0) -> float:
    """Peak action ideally at 60–80% of segment (build → payoff)."""
    gun = float(getattr(metrics, "panns_gun_max", 0) or 0)
    motion = float(getattr(metrics, "center_motion", 0) or 0)
    action = min(1.0, gun * 1.4 + motion * 2.0)
    clip = max(0.0, float(getattr(metrics, "clip_score", 0) or 0))
    return round(min(1.0, action * 0.7 + clip * 0.3), 4)


def segment_viral_score(metrics: Any, video_path: Path | None = None) -> float:
    """action * hook * novelty * (1 - menu_penalty)."""
    profile = getattr(metrics, "profile", "pubg")
    hook = float(getattr(metrics, "hook_score", 0) or 0)
    if hook <= 0 and video_path is not None:
        hook, _ = hook_score(video_path, float(metrics.start), profile)
        metrics.hook_score = hook

    action = min(
        1.0,
        float(getattr(metrics, "panns_gun_max", 0)) * 0.5
        + max(0.0, float(getattr(metrics, "clip_score", 0))) * 0.35
        + float(getattr(metrics, "center_motion", 0)) * 0.15,
    )
    heat = float(getattr(metrics, "heatmap_intensity", 0) or 0)
    novelty = 0.85 + heat * 0.15
    payoff = payoff_timing_score(metrics)
    menu_pen = 0.0
    if hook < SEGMENT_HOOK_MIN:
        menu_pen = 0.25

    viral = action * hook * novelty * payoff * (1.0 - menu_pen)
    return round(min(1.0, max(0.0, viral)), 4)


def segment_hook_ok(metrics: dict) -> bool:
    hook = float(metrics.get("hook_score", 0) or 0)
    return hook >= SEGMENT_HOOK_MIN


def montage_viral_score(segments: list[dict]) -> tuple[float, bool]:
    """First segment must pass hook threshold (frame 1 = action)."""
    if not segments:
        return 0.0, False
    scores = []
    for seg in segments:
        hm = seg.get("highlight_metrics") or seg.get("strict_metrics") or {}
        scores.append(float(hm.get("viral_score", hm.get("combined_score", 0))))
    first_hook = float(
        (segments[0].get("highlight_metrics") or segments[0].get("strict_metrics") or {}).get(
            "hook_score", 0
        )
    )
    hook_ok = first_hook >= HOOK_MIN_SCORE
    return round(float(np.mean(scores)) if scores else 0.0, 4), hook_ok


def trim_segment_start(
    video_path: Path,
    start_sec: float,
    profile: str,
    *,
    max_trim: float = 3.0,
    window_sec: float = 10.0,
) -> float:
    """Shift start forward to first gun/action spike (max +3s trim)."""
    from highlight_scorer import score_panns_audio, PANN_GUN_MIN, normalize_profile

    profile = normalize_profile(profile)
    if profile not in ("pubg", "standoff", "wot"):
        return start_sec

    best = start_sec
    best_gun = 0.0
    step = 0.5
    t = 0.0
    while t <= max_trim:
        probe_start = start_sec + t
        panns = score_panns_audio(video_path, probe_start, min(2.5, window_sec))
        gun = panns.get("panns_gun_max", 0.0)
        if gun > best_gun and gun >= PANN_GUN_MIN * 0.6:
            best_gun = gun
            best = probe_start
        if gun >= PANN_GUN_MIN:
            return round(probe_start, 3)
        t += step
    return round(best, 3)
