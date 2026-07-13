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
    if motion.size < 4:
        return 0.02
    return max(0.018, float(np.percentile(motion, 55)) * 0.85)


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


def _hud_probe_cap() -> int:
    return max(8, int(os.environ.get("MLBB_TEAMFIGHT_HUD_CAP", "32")))


def rank_starts_by_teamfight(
    analysis: dict[str, Any],
    starts: list[float],
    *,
    video_path: Path | None = None,
    banner_tiers: dict[float, int] | None = None,
) -> list[float]:
    """Re-rank stage-1 starts by combined teamfight score."""
    if not starts:
        return []
    tiers = banner_tiers or {}
    use_hud = video_path is not None and os.environ.get("MLBB_TEAMFIGHT_HUD", "1") == "1"
    hud_cap = _hud_probe_cap()
    hud_starts: set[float] = set()
    if use_hud and len(starts) > hud_cap:
        cheap = sorted(
            ((score_teamfight_bins(analysis, start), start) for start in starts),
            key=lambda row: row[0],
            reverse=True,
        )
        hud_starts = {start for _, start in cheap[:hud_cap]}

    scored: list[tuple[float, float]] = []
    for start in starts:
        tier = tiers.get(round(start, 1)) or tiers.get(start)
        probe_path = video_path if (use_hud and (not hud_starts or start in hud_starts)) else None
        sc = combined_teamfight_score(
            analysis,
            start,
            video_path=probe_path,
            banner_tier=tier,
        )
        scored.append((sc, start))
    scored.sort(key=lambda row: row[0], reverse=True)
    return [s for sc, s in scored if sc >= _min_teamfight_score() * 0.5] or [s for _, s in scored]
