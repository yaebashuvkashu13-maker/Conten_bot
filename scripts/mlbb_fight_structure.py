#!/usr/bin/env python3
"""MLBB fight-structure gate — hero teamfight vs creep lane farm."""

from __future__ import annotations

import os
from pathlib import Path

import numpy as np


def _gate_enabled() -> bool:
    return os.environ.get("MLBB_HERO_FIGHT_GATE", "1") == "1"


def _timeline_bins() -> int:
    return max(3, int(os.environ.get("MLBB_HERO_FIGHT_BINS", "5")))


def _creep_motion_min() -> float:
    return float(os.environ.get("MLBB_CREEP_FARM_MOTION_MIN", "0.022"))


def _creep_mini_max() -> float:
    return float(os.environ.get("MLBB_CREEP_FARM_MINI_MAX", "0.009"))


def _creep_skill_max() -> float:
    return float(os.environ.get("MLBB_CREEP_FARM_SKILL_MAX", "0.008"))


def _hero_mini_min() -> float:
    return float(os.environ.get("MLBB_HERO_FIGHT_MIN_MINI", "0.011"))


def _hero_skill_min() -> float:
    return float(os.environ.get("MLBB_HERO_FIGHT_MIN_SKILL", "0.010"))


def _hero_combat_bins_min() -> int:
    return max(1, int(os.environ.get("MLBB_HERO_FIGHT_MIN_COMBAT_BINS", "2")))


def _peak_lift_ratio() -> float:
    return float(os.environ.get("MLBB_HERO_FIGHT_PEAK_LIFT", "1.12"))


def _bin_combat_score(motion: float, mini: float, skill: float) -> float:
    return float(motion * 1.8 + mini * 4.2 + skill * 3.6)


def analyze_fight_timeline(
    video_path: Path | str,
    start_sec: float,
    duration_sec: float,
    *,
    crop_box: tuple[int, int, int, int] | None = None,
    peak_start: float | None = None,
    sample_bins: int | None = None,
) -> dict:
    """
    Sample combat metrics across the clip window.

    Returns per-bin motion/mini/skill plus aggregate fight-shape stats.
    """
    from gameplay_gate import score_segment_combat

    video_path = Path(video_path)
    dur = max(0.8, float(duration_sec))
    bins = sample_bins or _timeline_bins()
    edges = np.linspace(float(start_sec), float(start_sec) + dur, num=bins + 1)
    rows: list[dict[str, float]] = []
    for i in range(bins):
        t0 = float(edges[i])
        t1 = float(edges[i + 1])
        bin_dur = max(0.35, t1 - t0)
        motion, mini, skill, _text = score_segment_combat(
            video_path, t0, bin_dur, crop_box=crop_box, sample_frames=4
        )
        rows.append(
            {
                "start": round(t0, 2),
                "motion": float(motion),
                "mini": float(mini),
                "skill": float(skill),
                "combat": _bin_combat_score(motion, mini, skill),
            }
        )

    motions = [r["motion"] for r in rows]
    minis = [r["mini"] for r in rows]
    skills = [r["skill"] for r in rows]
    combats = [r["combat"] for r in rows]

    peak_idx = len(rows) // 2
    if peak_start is not None:
        peak_idx = int(
            round(
                max(0.0, min(1.0, (float(peak_start) - float(start_sec)) / dur))
                * max(0, len(rows) - 1)
            )
        )

    hero_bins = sum(
        1
        for r in rows
        if r["mini"] >= _hero_mini_min() or r["skill"] >= _hero_skill_min()
    )
    creep_bins = sum(
        1
        for r in rows
        if r["motion"] >= _creep_motion_min()
        and r["mini"] < _creep_mini_max()
        and r["skill"] < _creep_skill_max()
    )
    baseline = float(np.mean(combats)) if combats else 0.0
    peak_combat = float(rows[peak_idx]["combat"]) if rows else 0.0
    hud_activity = float(np.mean(minis) + np.mean(skills)) if rows else 0.0

    return {
        "bins": rows,
        "peak_idx": peak_idx,
        "hero_bins": hero_bins,
        "creep_bins": creep_bins,
        "avg_motion": float(np.mean(motions)) if motions else 0.0,
        "avg_mini": float(np.mean(minis)) if minis else 0.0,
        "avg_skill": float(np.mean(skills)) if skills else 0.0,
        "baseline_combat": baseline,
        "peak_combat": peak_combat,
        "peak_lift": (peak_combat / baseline) if baseline > 1e-6 else 0.0,
        "hud_activity": hud_activity,
    }


def passes_hero_fight_gate(
    video_path: Path | str,
    start_sec: float,
    duration_sec: float,
    *,
    peak_start: float | None = None,
    multikill: bool = False,
    crop_box: tuple[int, int, int, int] | None = None,
) -> tuple[bool, str, dict]:
    """
    Reject lane creep farm disguised as kill banner.

    Creep farm: visible center motion (last hits) but flat minimap + skill bar.
    Hero fight: several bins with HUD activity and a combat lift around the kill peak.
    """
    if not _gate_enabled():
        return True, "hero_fight_disabled", {}

    video_path = Path(video_path)
    if crop_box is None:
        try:
            from gameplay_gate import detect_game_viewport_crop

            crop_box = detect_game_viewport_crop(video_path, start_sec, duration_sec)
        except ImportError:
            crop_box = None

    timeline = analyze_fight_timeline(
        video_path,
        start_sec,
        duration_sec,
        crop_box=crop_box,
        peak_start=peak_start,
    )
    bins = timeline["bins"]
    if not bins:
        return False, "hero_fight_no_bins", timeline

    creep_ratio = timeline["creep_bins"] / max(1, len(bins))
    hero_bins = int(timeline["hero_bins"])
    min_combat_bins = _hero_combat_bins_min()
    if multikill:
        min_combat_bins = max(1, min_combat_bins - 1)

    creep_motion = _creep_motion_min()
    creep_mini = _creep_mini_max() * (1.15 if multikill else 1.0)
    creep_skill = _creep_skill_max() * (1.15 if multikill else 1.0)

    if (
        timeline["avg_motion"] >= creep_motion
        and timeline["avg_mini"] < creep_mini
        and timeline["avg_skill"] < creep_skill
        and hero_bins < min_combat_bins
    ):
        return (
            False,
            (
                f"creep_farm motion={timeline['avg_motion']:.3f} "
                f"mini={timeline['avg_mini']:.3f} skill={timeline['avg_skill']:.3f}"
            ),
            timeline,
        )

    if creep_ratio >= 0.6 and hero_bins < min_combat_bins:
        return (
            False,
            f"creep_shape bins={timeline['creep_bins']}/{len(bins)} hero_bins={hero_bins}",
            timeline,
        )

    if hero_bins < min_combat_bins:
        return False, f"low_hud_bins={hero_bins}", timeline

    peak_lift_min = _peak_lift_ratio()
    if multikill:
        peak_lift_min *= 0.92
    if timeline["peak_lift"] < peak_lift_min and timeline["hud_activity"] < 0.020:
        return (
            False,
            f"flat_peak lift={timeline['peak_lift']:.2f} hud={timeline['hud_activity']:.3f}",
            timeline,
        )

    return True, "hero_fight_ok", timeline
