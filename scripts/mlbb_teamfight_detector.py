#!/usr/bin/env python3
"""MLBB teamfight scoring — minimap + skills + motion, blended with kill banner tier."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import numpy as np

TIER_WEIGHT = {
    1: 0.30,
    2: 0.55,
    3: 0.75,
    4: 0.88,
    5: 1.0,
}


def _min_teamfight_score() -> float:
    return float(os.environ.get("MLBB_TEAMFIGHT_MIN_SCORE", "0.45"))


def _motion_threshold(analysis: dict[str, Any]) -> float:
    motion = np.asarray(analysis.get("center_motion", []), dtype=np.float32)
    abs_floor = float(
        os.environ.get("MLBB_TEAMFIGHT_ABS_MOTION", os.environ.get("MLBB_FIGHT_MIN_MOTION", "0.038"))
    )
    if motion.size < 4:
        return abs_floor
    # Relative thr helps on loud VODs, but never below a real fight floor —
    # otherwise a calm farming VOD normalizes to "everything is a fight".
    return max(abs_floor, float(np.percentile(motion, 55)) * 0.85)


def _sustain_bins(analysis: dict[str, Any], start: float, *, min_bins: int = 2) -> float:
    """Fraction of window bins above motion threshold (proxy for sustained fight)."""
    win = float(analysis.get("window_seconds", 2.0))
    motion = np.asarray(analysis.get("center_motion", []), dtype=np.float32)
    if motion.size == 0:
        return 0.0
    i0 = max(0, int(start / win))
    i1 = min(len(motion), int((start + 10.0) / win))
    if i1 <= i0:
        return 0.0
    thr = _motion_threshold(analysis)
    chunk = motion[i0:i1]
    active = float(np.sum(chunk >= thr)) / float(max(len(chunk), 1))
    need = max(1, min_bins)
    if float(np.sum(chunk >= thr)) < need:
        return active * 0.6
    return active


def score_teamfight_bins(analysis: dict[str, Any], start: float) -> float:
    win = float(analysis.get("window_seconds", 2.0))
    motion = np.asarray(analysis.get("center_motion", []), dtype=np.float32)
    audio = np.asarray(analysis.get("audio", []), dtype=np.float32)
    if motion.size == 0:
        return 0.0
    i0 = max(0, int(start / win))
    i1 = min(len(motion), int((start + 10.0) / win))
    if i1 <= i0:
        return 0.0
    m_peak = float(np.max(motion[i0:i1]))
    a_peak = float(np.max(audio[i0:i1])) if audio.size else 0.0
    sustain = _sustain_bins(analysis, start, min_bins=int(os.environ.get("MLBB_TEAMFIGHT_SUSTAIN_BINS", "2")))
    thr = _motion_threshold(analysis)
    motion_ok = 1.0 if m_peak >= thr else m_peak / max(thr, 1e-6)
    return min(1.0, motion_ok * 0.45 + sustain * 0.35 + min(1.0, a_peak * 2.5) * 0.20)


def score_teamfight_hud(
    video_path: Path,
    start: float,
    duration_sec: float = 10.0,
    *,
    crop_box: tuple[int, int, int, int] | None = None,
) -> tuple[float, float, float]:
    from gameplay_gate import score_segment_combat

    motion, mini, skill, _text = score_segment_combat(
        video_path,
        start,
        duration_sec,
        crop_box=crop_box,
        sample_frames=6,
    )
    mini_thr = float(os.environ.get("MLBB_TEAMFIGHT_MIN_MINIMAP", "0.012"))
    skill_thr = float(os.environ.get("MLBB_TEAMFIGHT_MIN_SKILL", "0.007"))
    motion_thr = float(os.environ.get("MLBB_TEAMFIGHT_MIN_MOTION", "0.038"))
    mini_ok = min(1.0, mini / max(mini_thr, 1e-6))
    skill_ok = min(1.0, skill / max(skill_thr, 1e-6))
    motion_ok = min(1.0, motion / max(motion_thr, 1e-6))
    sustain_bonus = 0.12 if mini >= mini_thr and duration_sec >= 3.0 else 0.0
    score = min(1.0, mini_ok * 0.38 + skill_ok * 0.28 + motion_ok * 0.34 + sustain_bonus)
    return score, mini, skill


def banner_tier_weight(tier: int | str | None) -> float:
    if tier is None:
        return 0.0
    if isinstance(tier, str):
        mapping = {"single": 1, "double": 2, "triple": 3, "maniac": 4, "savage": 5}
        tier = mapping.get(tier.strip().lower(), 0)
    return TIER_WEIGHT.get(int(tier), 0.0)


def combined_teamfight_score(
    analysis: dict[str, Any],
    start: float,
    *,
    video_path: Path | None = None,
    banner_tier: int | str | None = None,
    duration_sec: float = 10.0,
) -> float:
    bin_score = score_teamfight_bins(analysis, start)
    hud_score = bin_score
    if video_path is not None and os.environ.get("MLBB_TEAMFIGHT_HUD", "1") == "1":
        try:
            hud_score, _, _ = score_teamfight_hud(video_path, start, duration_sec)
        except Exception:
            hud_score = bin_score
    teamfight = hud_score * 0.55 + bin_score * 0.45
    tier_w = banner_tier_weight(banner_tier)
    return teamfight * 0.6 + tier_w * 0.4


def passes_teamfight_threshold(score: float) -> bool:
    return score >= _min_teamfight_score()


def rank_starts_by_teamfight(
    analysis: dict[str, Any],
    starts: list[float],
    *,
    video_path: Path | None = None,
    banner_tiers: dict[float, int] | None = None,
) -> list[float]:
    """Re-rank stage-1 starts by combined teamfight score.

    Never fall back to the full unfiltered list — that re-admitted farming /
    recall junk after a banner miss.
    """
    if not starts:
        return []
    tiers = banner_tiers or {}
    scored: list[tuple[float, float]] = []
    for start in starts:
        tier = tiers.get(round(start, 1)) or tiers.get(start)
        sc = combined_teamfight_score(
            analysis,
            start,
            video_path=video_path,
            banner_tier=tier,
        )
        scored.append((sc, start))
    scored.sort(key=lambda row: row[0], reverse=True)
    frac = float(os.environ.get("MLBB_TEAMFIGHT_RANK_FRAC", "0.75"))
    min_keep = _min_teamfight_score() * max(0.35, min(1.0, frac))
    kept = [s for sc, s in scored if sc >= min_keep]
    if kept:
        max_n = max(1, int(os.environ.get("MLBB_TEAMFIGHT_RANK_MAX", "8")))
        return kept[:max_n]
    floor = float(os.environ.get("MLBB_TEAMFIGHT_ABS_FLOOR", "0.30"))
    if scored and scored[0][0] >= floor:
        return [scored[0][1]]
    return []


def fight_first_peaks(
    analysis: dict[str, Any],
    starts: list[float] | None = None,
    *,
    limit: int | None = None,
    min_score: float | None = None,
) -> list[float]:
    """
    Cheap fight-first ranking: motion/audio bins only (no HUD decode).

    Used to decide WHERE to look for kill banners — banner OCR runs after
    a fight peak, not as a blind dense sweep of the whole VOD.
    """
    if starts is None:
        starts = []
    if not starts:
        # Derive peaks from analysis motion when stage1 list is empty.
        win = float(analysis.get("window_seconds", 2.0))
        motion = np.asarray(analysis.get("center_motion", []), dtype=np.float32)
        audio = np.asarray(analysis.get("audio", []), dtype=np.float32)
        if motion.size == 0:
            return []
        combined = motion * 0.55 + (audio * 0.45 if audio.size == motion.size else 0.0)
        order = np.argsort(combined)[::-1]
        gap = float(os.environ.get("MLBB_FIGHT_FIRST_MIN_GAP_SEC", "18"))
        skip = float(os.environ.get("MLBB_VOD_MIN_PEAK_SEC", "45"))
        picked: list[float] = []
        for idx in order:
            t = float(idx) * win
            if t < skip:
                continue
            if any(abs(t - s) < gap for s in picked):
                continue
            picked.append(round(t, 1))
            if len(picked) >= int(os.environ.get("MLBB_BANNER_FIGHT_FIRST_POOL", "32")):
                break
        starts = picked
    if not starts:
        return []

    floor = float(
        min_score
        if min_score is not None
        else os.environ.get("MLBB_FIGHT_FIRST_MIN_SCORE", os.environ.get("MLBB_TEAMFIGHT_ABS_FLOOR", "0.28"))
    )
    cap = max(
        1,
        int(limit if limit is not None else os.environ.get("MLBB_BANNER_FIGHT_FIRST_PEAKS", "12")),
    )
    scored: list[tuple[float, float]] = []
    for start in starts:
        sc = float(score_teamfight_bins(analysis, float(start)))
        if sc < floor:
            continue
        scored.append((sc, float(start)))
    scored.sort(key=lambda row: (-row[0], row[1]))
    if not scored:
        # Keep strongest peaks even if under floor — still better than blind dense.
        soft: list[tuple[float, float]] = [
            (float(score_teamfight_bins(analysis, float(s))), float(s)) for s in starts
        ]
        soft.sort(key=lambda row: (-row[0], row[1]))
        return [t for _, t in soft[:cap]]
    return [t for _, t in scored[:cap]]


def metrics_combat_score(
    *,
    center_motion: float,
    minimap_delta: float,
    skill_delta: float,
) -> float:
    """Cheap combat proxy from already-scored highlight metrics (0..1)."""
    motion_min = float(os.environ.get("MLBB_FIGHT_MIN_MOTION", "0.038"))
    mini_min = float(os.environ.get("MLBB_FIGHT_MIN_MINIMAP", "0.016"))
    skill_min = float(os.environ.get("MLBB_FIGHT_MIN_SKILL", "0.014"))
    return min(
        1.0,
        min(1.0, float(center_motion) / max(motion_min, 1e-6)) * 0.40
        + min(1.0, float(minimap_delta) / max(mini_min, 1e-6)) * 0.35
        + min(1.0, float(skill_delta) / max(skill_min, 1e-6)) * 0.25,
    )
