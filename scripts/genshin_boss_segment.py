#!/usr/bin/env python3
"""Genshin boss segment validation — require boss HP bar in fight windows."""

from __future__ import annotations

import os
from pathlib import Path


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
