#!/usr/bin/env python3
"""Single hard gate for PUBG/Standoff combat segments — shooting only."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from gameplay_gate import detect_game_viewport_crop, segment_looks_like_pubg_loot_or_walk, _read_frame_at
from pubg_shooting_gate import pubg_passes_shooting_gate
from visual_action_check import check_frame_visual, segment_frame_times

COMBAT_PROFILES = frozenset({"pubg", "standoff"})
PANN_ABSOLUTE_MIN = float(os.environ.get("PUBG_COMBAT_PANN_MIN", "0.22"))
MIN_HIT_FLASH_ANY = float(os.environ.get("PUBG_COMBAT_MIN_HIT_FLASH", "0.004"))
MIN_WEAPON_EDGE_ANY = float(os.environ.get("PUBG_COMBAT_MIN_WEAPON_EDGE", "0.025"))
FRAMES_REQUIRED = int(os.environ.get("PUBG_COMBAT_FRAMES_REQUIRED", "3"))


def _norm_profile(profile: str) -> str:
    p = profile.strip().lower()
    return "standoff" if p == "standoff" else "pubg" if p == "pubg" else p


def pubg_combat_visual_strict(
    video_path: Path,
    start_sec: float,
    duration_sec: float,
    profile: str,
) -> tuple[bool, str, dict[str, Any]]:
    """3/3 frames pass visual; at least one frame has hit_flash or weapon_edge."""
    profile = _norm_profile(profile)
    if profile not in COMBAT_PROFILES:
        return False, "not_combat_profile", {}

    crop = detect_game_viewport_crop(video_path, start_sec, duration_sec)
    frames_out: list[dict] = []
    passed = 0
    best_flash = 0.0
    best_weapon = 0.0

    for label, t in segment_frame_times(start_sec, duration_sec):
        frame = _read_frame_at(video_path, t)
        if frame is None:
            frames_out.append({"label": label, "pass": False, "reason": "frame_missing"})
            continue
        if crop is not None:
            x, y, w, h = crop
            frame = frame[y : y + h, x : x + w]
        ok, reason, fmetrics = check_frame_visual(profile, frame)
        flash = float(fmetrics.get("hit_flash", 0))
        weapon = float(fmetrics.get("weapon_edge", 0))
        best_flash = max(best_flash, flash)
        best_weapon = max(best_weapon, weapon)
        if ok:
            passed += 1
        frames_out.append(
            {
                "label": label,
                "pass": ok,
                "reason": reason,
                "hit_flash": flash,
                "weapon_edge": weapon,
            }
        )

    need = FRAMES_REQUIRED
    if passed < need:
        bad = [f"{f['label']}:{f.get('reason', '?')}" for f in frames_out if not f.get("pass")]
        return False, f"visual_frames={passed}/{need}:{','.join(bad[:3])}", {
            "frames_passed": passed,
            "frames_required": need,
            "frames": frames_out,
        }

    if best_flash < MIN_HIT_FLASH_ANY and best_weapon < MIN_WEAPON_EDGE_ANY:
        return False, (
            f"no_combat_signal flash={best_flash:.4f} weapon={best_weapon:.4f}"
        ), {
            "frames_passed": passed,
            "best_hit_flash": best_flash,
            "best_weapon_edge": best_weapon,
            "frames": frames_out,
        }

    return True, "combat_visual_strict", {
        "frames_passed": passed,
        "best_hit_flash": best_flash,
        "best_weapon_edge": best_weapon,
        "frames": frames_out,
    }


def pubg_passes_combat_gate(
    video_path: Path,
    start_sec: float,
    duration_sec: float,
    profile: str,
    *,
    metrics: Any | None = None,
) -> tuple[bool, str, dict[str, Any]]:
    """
    PASS only if ALL:
    1. pubg_passes_shooting_gate (gun >= 0.055, burst >= 4.8)
    2. PANNs gun_max >= max(0.22, calibrated_threshold)
    3. visual 3/3 + hit_flash/weapon_edge on >=1 frame
    4. NOT segment_looks_like_pubg_loot_or_walk
    """
    profile = _norm_profile(profile)
    if profile not in COMBAT_PROFILES:
        return False, "not_combat_profile", {}

    out: dict[str, Any] = {"start": round(start_sec, 3), "duration": round(duration_sec, 3)}

    shoot_ok, shoot_reason, shoot_row = pubg_passes_shooting_gate(
        video_path, start_sec, duration_sec
    )
    out.update(shoot_row)
    if not shoot_ok:
        return False, shoot_reason, out

    from highlight_scorer import (
        PANN_GUN_INFERENCE_FLOOR,
        calibrated_pann_gun_min,
        score_panns_audio,
    )

    if metrics is not None and getattr(metrics, "panns_gun_max", 0) > 0:
        panns_gun = float(metrics.panns_gun_max)
        panns_thr = float(
            getattr(metrics, "panns_gun_threshold", 0) or calibrated_pann_gun_min(video_path, profile)
        )
    else:
        panns = score_panns_audio(video_path, start_sec, duration_sec)
        panns_gun = float(panns.get("panns_gun_max", 0))
        panns_thr = calibrated_pann_gun_min(video_path, profile)

    floor = max(PANN_GUN_INFERENCE_FLOOR, panns_thr, PANN_ABSOLUTE_MIN)
    out["panns_gun_max"] = round(panns_gun, 4)
    out["panns_gun_threshold"] = round(floor, 4)
    if panns_gun < floor:
        return False, f"panns_gun_low={panns_gun:.3f}:floor{floor:.3f}", out

    vis_ok, vis_reason, vis_row = pubg_combat_visual_strict(
        video_path, start_sec, duration_sec, profile
    )
    out["combat_visual"] = vis_row
    if not vis_ok:
        return False, vis_reason, out

    gun_density = float(shoot_row.get("gunfire_density", 0))
    crop = tuple(shoot_row["crop_box"]) if shoot_row.get("crop_box") else None
    if crop is not None:
        crop = tuple(int(v) for v in crop)
    if segment_looks_like_pubg_loot_or_walk(
        video_path,
        start_sec,
        duration_sec,
        crop_box=crop,
        gunfire_density=gun_density,
    ):
        return False, f"loot_walk=density{gun_density:.3f}", out

    out["pass"] = True
    return True, f"combat_ok=gun{panns_gun:.3f}:burst{shoot_row.get('burst_ratio')}", out
