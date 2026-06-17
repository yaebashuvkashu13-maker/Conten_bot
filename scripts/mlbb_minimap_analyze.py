#!/usr/bin/env python3
"""
MLBB minimap teamfight density — HSV player-dot detection (mlbb_analyze_move inspired).

Principle: crop minimap ROI → HSV masks for blue/red team dots → count + motion
between frames → high density + movement ≈ teamfight (works for live and replay).
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from pathlib import Path

import cv2
import numpy as np

# Portrait phone capture — minimap bottom-left (matches gameplay_gate / mlbb_hud_signals).
MINIMAP_ROI = (0.0, 0.72, 0.28, 1.0)

BLUE_HSV = (np.array([95, 150, 150]), np.array([125, 255, 255]))
RED_HSV_A = (np.array([0, 150, 150]), np.array([10, 255, 255]))
RED_HSV_B = (np.array([170, 150, 150]), np.array([180, 255, 255]))

PLAYER_MIN_AREA = 50
PLAYER_MAX_AREA = 400
PLAYER_MIN_CIRCULARITY = 0.55


@dataclass
class MinimapSignals:
    ally_dots: float = 0.0
    enemy_dots: float = 0.0
    total_dots: float = 0.0
    motion: float = 0.0
    teamfight_density: float = 0.0
    clash_score: float = 0.0

    def to_dict(self) -> dict:
        return {k: round(float(v), 4) for k, v in asdict(self).items()}


def _extract_minimap(frame: np.ndarray, roi: tuple[float, float, float, float]) -> np.ndarray:
    try:
        from video_orientation import resize_for_analysis
    except ImportError:
        resize_for_analysis = lambda f: cv2.resize(f, (320, 180))  # type: ignore

    small = resize_for_analysis(frame)
    h, w = small.shape[:2]
    x0, y0, x1, y1 = roi
    patch = small[int(h * y0) : int(h * y1), int(w * x0) : int(w * x1)]
    return patch


def _count_team_dots(minimap_bgr: np.ndarray) -> tuple[float, float]:
    if minimap_bgr.size == 0:
        return 0.0, 0.0
    hsv = cv2.cvtColor(minimap_bgr, cv2.COLOR_BGR2HSV)
    blurred = cv2.GaussianBlur(hsv, (5, 5), 0)

    blue_mask = cv2.inRange(blurred, BLUE_HSV[0], BLUE_HSV[1])
    red_mask = cv2.bitwise_or(
        cv2.inRange(blurred, RED_HSV_A[0], RED_HSV_A[1]),
        cv2.inRange(blurred, RED_HSV_B[0], RED_HSV_B[1]),
    )
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
    blue_mask = cv2.morphologyEx(blue_mask, cv2.MORPH_OPEN, kernel)
    red_mask = cv2.morphologyEx(red_mask, cv2.MORPH_OPEN, kernel)

    def _dots(mask: np.ndarray) -> float:
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        n = 0
        for c in contours:
            area = cv2.contourArea(c)
            if area < PLAYER_MIN_AREA or area > PLAYER_MAX_AREA:
                continue
            peri = cv2.arcLength(c, True)
            if peri <= 0:
                continue
            circ = 4 * np.pi * area / (peri * peri)
            if circ < PLAYER_MIN_CIRCULARITY:
                continue
            n += 1
        return float(min(n, 5))

    return _dots(blue_mask), _dots(red_mask)


def _mask_motion(a: np.ndarray, b: np.ndarray) -> float:
    if a.size == 0 or b.size == 0 or a.shape != b.shape:
        return 0.0
    hsv_a = cv2.cvtColor(a, cv2.COLOR_BGR2HSV)
    hsv_b = cv2.cvtColor(b, cv2.COLOR_BGR2HSV)
    combined_a = cv2.bitwise_or(
        cv2.inRange(hsv_a, BLUE_HSV[0], BLUE_HSV[1]),
        cv2.inRange(hsv_a, RED_HSV_A[0], RED_HSV_A[1]),
    )
    combined_b = cv2.bitwise_or(
        cv2.inRange(hsv_b, BLUE_HSV[0], BLUE_HSV[1]),
        cv2.inRange(hsv_b, RED_HSV_A[0], RED_HSV_A[1]),
    )
    diff = cv2.absdiff(combined_a, combined_b)
    return float(np.count_nonzero(diff)) / float(diff.size)


def score_minimap_teamfight(
    video_path: Path | str,
    start_sec: float,
    duration_sec: float,
    *,
    sample_frames: int | None = None,
) -> MinimapSignals:
    from mlbb_kill_ui import _sample_frames

    path = Path(video_path)
    n = sample_frames or int(os.environ.get("MLBB_MINIMAP_SAMPLES", "6"))
    frames = _sample_frames(path, start_sec, duration_sec, n)
    if not frames:
        return MinimapSignals()

    ally_vals: list[float] = []
    enemy_vals: list[float] = []
    patches: list[np.ndarray] = []
    for frame in frames:
        patch = _extract_minimap(frame, MINIMAP_ROI)
        if patch.size == 0:
            continue
        patches.append(patch)
        a, e = _count_team_dots(patch)
        ally_vals.append(a)
        enemy_vals.append(e)

    if not patches:
        return MinimapSignals()

    ally = float(np.mean(ally_vals))
    enemy = float(np.mean(enemy_vals))
    total = ally + enemy
    motions = [_mask_motion(patches[i - 1], patches[i]) for i in range(1, len(patches))]
    motion = float(np.mean(motions)) if motions else 0.0

    # Teamfight: many dots + minimap activity (both teams present is stronger).
    density = min(1.0, total / 6.0 + motion * 2.5)
    clash = min(1.0, min(ally, enemy) * 0.35 + motion * 2.0 + (total / 8.0))

    return MinimapSignals(
        ally_dots=ally,
        enemy_dots=enemy,
        total_dots=total,
        motion=motion,
        teamfight_density=density,
        clash_score=clash,
    )


def minimap_learning_boost(signals: MinimapSignals) -> float:
    return float(signals.teamfight_density * 0.1 + signals.clash_score * 0.08)
