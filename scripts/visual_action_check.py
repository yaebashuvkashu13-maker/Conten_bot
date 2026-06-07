#!/usr/bin/env python3
"""Visual-only action proof — audio/UI metrics are not sufficient."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from gameplay_gate import (
    _band_overlay_text_score,
    _frame_hud_metrics,
    _genshin_boss_bar_score,
    _read_frame_at,
    detect_game_viewport_crop,
)

FIVE_GAMES = frozenset({"pubg", "standoff", "wot", "world_of_tanks", "genshin", "mobile_legends", "mlbb"})


def _norm_profile(profile: str) -> str:
    p = profile.strip().lower()
    if p == "mlbb":
        return "mobile_legends"
    if p == "world_of_tanks":
        return "wot"
    return p


def _laplacian_edge_score(frame: np.ndarray, y0: float, y1: float, x0: float, x1: float) -> float:
    small = cv2.resize(frame, (320, 180))
    gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
    h, w = gray.shape
    roi = gray[int(h * y0) : int(h * y1), int(w * x0) : int(w * x1)]
    if roi.size == 0:
        return 0.0
    lap = cv2.Laplacian(roi, cv2.CV_64F)
    return float(lap.var()) / 10000.0


def _frame_hit_flash_score(frame: np.ndarray) -> float:
    """Bright transient pixels — muzzle / explosion feedback."""
    small = cv2.resize(frame, (320, 180))
    hsv = cv2.cvtColor(small, cv2.COLOR_BGR2HSV)
    sat = hsv[:, :, 1]
    val = hsv[:, :, 2]
    center = val[int(180 * 0.25) : int(180 * 0.70), int(320 * 0.20) : int(320 * 0.80)]
    if center.size == 0:
        return 0.0
    hot = (center > 210) & (sat > 45)
    return float(np.count_nonzero(hot)) / float(center.size)


def _frame_menu_overlay(frame: np.ndarray) -> float:
    return _band_overlay_text_score(frame, 0.18, 0.82)


def check_frame_visual(profile: str, frame: np.ndarray) -> tuple[bool, str, dict[str, Any]]:
    profile = _norm_profile(profile)
    metrics: dict[str, Any] = {}

    menu = _frame_menu_overlay(frame)
    metrics["menu_overlay"] = round(menu, 4)
    if menu > 0.42:
        return False, "menu_overlay", metrics

    center_edge = _laplacian_edge_score(frame, 0.22, 0.72, 0.12, 0.88)
    weapon_edge = _laplacian_edge_score(frame, 0.55, 0.92, 0.30, 0.70)
    flash = _frame_hit_flash_score(frame)
    metrics["center_edge"] = round(center_edge, 4)
    metrics["weapon_edge"] = round(weapon_edge, 4)
    metrics["hit_flash"] = round(flash, 4)

    mini_std, skill_std, top_std = _frame_hud_metrics(frame)
    metrics["minimap_std"] = round(mini_std, 3)
    metrics["skill_std"] = round(skill_std, 3)

    if profile in ("pubg", "standoff"):
        min_center = float(os.environ.get("VISUAL_PUBG_MIN_CENTER_EDGE", "0.028"))
        min_weapon = float(os.environ.get("VISUAL_PUBG_MIN_WEAPON_EDGE", "0.018"))
        min_flash = float(os.environ.get("VISUAL_PUBG_MIN_HIT_FLASH", "0.0015"))
        combat = center_edge >= min_center or weapon_edge >= min_weapon or flash >= min_flash
        if not combat:
            return False, "no_visible_combat", metrics
        sky_edge = _laplacian_edge_score(frame, 0.02, 0.35, 0.10, 0.90)
        ground_edge = _laplacian_edge_score(frame, 0.45, 0.95, 0.10, 0.90)
        metrics["sky_edge"] = round(sky_edge, 4)
        metrics["ground_edge"] = round(ground_edge, 4)
        if sky_edge > ground_edge * 1.6 and center_edge < min_center * 1.1 and flash < min_flash * 2:
            return False, "sky_pan_no_fight", metrics
        return True, "combat_visible", metrics

    if profile == "mobile_legends":
        min_mini = float(os.environ.get("VISUAL_MLBB_MIN_MINIMAP_STD", "7.5"))
        min_skill = float(os.environ.get("VISUAL_MLBB_MIN_SKILL_STD", "6.5"))
        min_center = float(os.environ.get("VISUAL_MLBB_MIN_CENTER_EDGE", "0.032"))
        if mini_std < min_mini or skill_std < min_skill:
            return False, "hud_missing", metrics
        if center_edge < min_center and flash < 0.002:
            return False, "no_fight_in_frame", metrics
        return True, "mlbb_fight_visible", metrics

    if profile == "genshin":
        boss_bar = _genshin_boss_bar_score(frame)
        metrics["boss_bar"] = round(boss_bar, 4)
        min_bar = float(os.environ.get("VISUAL_GENSHIN_MIN_BOSS_BAR", "0.22"))
        min_center = float(os.environ.get("VISUAL_GENSHIN_MIN_CENTER_EDGE", "0.030"))
        if boss_bar < min_bar and center_edge < min_center:
            return False, "no_boss_visible", metrics
        return True, "boss_visible", metrics

    if profile == "wot":
        min_center = float(os.environ.get("VISUAL_WOT_MIN_CENTER_EDGE", "0.026"))
        min_flash = float(os.environ.get("VISUAL_WOT_MIN_HIT_FLASH", "0.002"))
        if center_edge < min_center and flash < min_flash:
            return False, "no_impact_visible", metrics
        return True, "impact_visible", metrics

    return False, "unknown_profile", metrics


def segment_frame_times(start_sec: float, duration_sec: float) -> list[tuple[str, float]]:
    end = start_sec + max(duration_sec, 0.5)
    mid = start_sec + duration_sec * 0.5
    return [
        ("start", start_sec + 0.15),
        ("mid", mid),
        ("end", max(start_sec + 0.2, end - 0.25)),
    ]


def extract_and_check_segment(
    video_path: Path,
    start_sec: float,
    duration_sec: float,
    profile: str,
    *,
    crop_box: tuple[int, int, int, int] | None = None,
    metrics: dict | None = None,
) -> dict[str, Any]:
    profile = _norm_profile(profile)
    if crop_box is None:
        crop_box = detect_game_viewport_crop(video_path, start_sec, duration_sec)

    frames_out: list[dict] = []
    passed_frames = 0
    for label, t in segment_frame_times(start_sec, duration_sec):
        frame = _read_frame_at(video_path, t)
        if frame is None:
            frames_out.append(
                {
                    "label": label,
                    "timestamp": round(t, 3),
                    "pass": False,
                    "reason": "frame_missing",
                    "metrics": {},
                }
            )
            continue
        if crop_box is not None:
            x, y, w, h = crop_box
            frame = frame[y : y + h, x : x + w]
        ok, reason, fmetrics = check_frame_visual(profile, frame)
        if ok:
            passed_frames += 1
        frames_out.append(
            {
                "label": label,
                "timestamp": round(t, 3),
                "pass": ok,
                "reason": reason,
                "metrics": fmetrics,
            }
        )

    need = int(os.environ.get("VISUAL_MIN_FRAMES_PASS", "3"))
    if profile in ("pubg", "standoff"):
        need = int(os.environ.get("VISUAL_PUBG_MIN_FRAMES_PASS", "2"))
    seg_pass = passed_frames >= need
    fail_reason = ""
    if not seg_pass:
        bad = [f"{f['label']}:{f['reason']}" for f in frames_out if not f.get("pass")]
        fail_reason = ",".join(bad[:3])

    return {
        "start": round(start_sec, 3),
        "duration": round(duration_sec, 3),
        "profile": profile,
        "visual_pass": seg_pass,
        "frames_passed": passed_frames,
        "frames_required": need,
        "fail_reason": fail_reason,
        "frames": frames_out,
        "audio_metrics": metrics or {},
    }


def verify_segments_visual(
    video_path: Path,
    profile: str,
    segments: list[tuple[float, float]],
    *,
    segment_metrics: list[dict] | None = None,
) -> tuple[int, int, list[dict], str]:
    """Returns (passed_count, total, rows, refuse_reason)."""
    rows: list[dict] = []
    passed = 0
    for idx, (start, dur) in enumerate(segments):
        audio_m = (segment_metrics or [{}])[idx] if segment_metrics else {}
        row = extract_and_check_segment(
            video_path, start, dur, profile, metrics=audio_m
        )
        rows.append(row)
        if row["visual_pass"]:
            passed += 1

    if passed == len(segments):
        return passed, len(segments), rows, ""

    first_fail = next((r for r in rows if not r["visual_pass"]), None)
    reason = "visual_failed"
    if first_fail:
        reason = f"visual_failed seg@{first_fail['start']}:{first_fail.get('fail_reason', 'unknown')}"
    return passed, len(segments), rows, reason
