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


def validate_genshin_clip_head(
    video_path: Path,
    start_sec: float,
    *,
    head_sec: float | None = None,
    crop_box: tuple[int, int, int, int] | None = None,
) -> tuple[bool, str, dict]:
    """Reject long pre-fight flight: boss HP bar must appear in the clip head."""
    if os.environ.get("GENSHIN_REQUIRE_BAR_AT_START", "1") != "1":
        return True, "genshin_head_skip", {}
    head = float(
        head_sec
        if head_sec is not None
        else os.environ.get("GENSHIN_BAR_HEAD_SEC", "8")
    )
    head = max(3.0, min(head, 16.0))
    min_bar = float(os.environ.get("GENSHIN_BAR_HEAD_MIN", "0.18"))
    min_peak = float(os.environ.get("GENSHIN_BAR_HEAD_PEAK_MIN", "0.28"))
    from gameplay_gate import score_genshin_boss_likelihood

    boss_bar, _motion, boss_score, bar_peak = score_genshin_boss_likelihood(
        video_path,
        float(start_sec),
        head,
        crop_box=crop_box,
        sample_frames=6,
    )
    metrics = {
        "head_bar": round(boss_bar, 4),
        "head_bar_peak": round(bar_peak, 4),
        "head_boss_score": round(boss_score, 4),
        "head_sec": head,
    }
    if boss_bar < min_bar and bar_peak < min_peak:
        return False, f"genshin_no_bar_at_start=bar{boss_bar:.3f}", metrics
    return True, f"genshin_head_ok=bar{boss_bar:.3f}", metrics


def validate_genshin_boss_segment(
    video_path: Path,
    start_sec: float,
    duration_sec: float,
    *,
    crop_box: tuple[int, int, int, int] | None = None,
) -> tuple[bool, str, dict]:
    head_ok, head_reason, head_metrics = validate_genshin_clip_head(
        video_path, start_sec, crop_box=crop_box
    )
    if not head_ok:
        return False, head_reason, head_metrics

    boss_bar, bar_peak, extras = boss_bar_ratio_in_segment(
        video_path, start_sec, duration_sec, crop_box=crop_box
    )
    min_ratio = _min_bar_ratio()
    reject_ratio = _reject_explore_bar()
    metrics = {
        "boss_bar_ratio": round(boss_bar, 4),
        "boss_bar_peak": round(bar_peak, 4),
        "boss_score": round(extras[2], 4) if len(extras) > 2 else 0.0,
        **head_metrics,
    }
    if boss_bar < reject_ratio and bar_peak < reject_ratio * 1.1:
        return False, f"genshin_explore=bar{boss_bar:.3f}", metrics
    if boss_bar < min_ratio * 0.85 and bar_peak < min_ratio:
        return False, f"genshin_no_boss_bar=bar{boss_bar:.3f}:need{min_ratio:.2f}", metrics
    return True, f"genshin_boss_ok=bar{boss_bar:.3f}", metrics
