#!/usr/bin/env python3
"""Single hard gate for PUBG/Standoff combat segments — shooting only."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import numpy as np

from gameplay_gate import (
    _extract_segment_audio_pcm,
    detect_game_viewport_crop,
    score_pubg_gunfire_audio,
    score_segment_combat,
    segment_looks_like_pubg_loot_or_walk,
    _read_frame_at,
)
from pubg_shooting_gate import pubg_passes_shooting_gate
from visual_action_check import check_frame_visual, segment_frame_times

COMBAT_PROFILES = frozenset({"pubg", "standoff"})
TRAINING_UI_KEYWORDS = (
    "training",
    "practice",
    "train",
    "полигон",
    "трениров",
    "учебн",
    "bot",
    "vs ai",
    "aimlab",
    "разминк",
)
KILLFEED_KEYWORDS = ("kill", "knock", "eliminated", "headshot", "убил", "убийство", "нок")
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


def _gunfire_spike_indices(pcm: np.ndarray, *, frame: int = 256) -> list[int]:
    if pcm.size < frame * 4:
        return []
    samples = pcm.astype(np.float32) / 32768.0
    energies: list[float] = []
    for offset in range(0, len(samples) - frame, frame):
        chunk = samples[offset : offset + frame]
        energies.append(float(np.sqrt(np.mean(chunk * chunk))))
    if len(energies) < 3:
        return []
    arr = np.asarray(energies, dtype=np.float32)
    median = float(np.median(arr))
    floor = max(median * 2.6, 0.010)
    spikes: list[int] = []
    for idx in range(1, len(arr)):
        if arr[idx] > floor and arr[idx] > arr[idx - 1] * 1.55:
            spikes.append(idx)
    return spikes


def _count_burst_clusters(spike_indices: list[int], *, gap_frames: int = 18) -> int:
    if not spike_indices:
        return 0
    clusters = 1
    for idx in range(1, len(spike_indices)):
        if spike_indices[idx] - spike_indices[idx - 1] > gap_frames:
            clusters += 1
    return clusters


def _gunfire_pvp_shape(
    video_path: Path,
    start_sec: float,
    duration_sec: float,
) -> tuple[int, int, float]:
    """Return burst clusters, active quarters, and temporal span ratio (0..1)."""
    pcm = _extract_segment_audio_pcm(video_path, start_sec, duration_sec)
    spikes = _gunfire_spike_indices(pcm)
    clusters = _count_burst_clusters(spikes)
    span_ratio = 0.0
    if len(spikes) >= 2:
        span_ratio = (spikes[-1] - spikes[0]) / max(len(spikes), 1)
        span_ratio = min(1.0, span_ratio / 8.0)

    quarter = max(duration_sec / 4.0, 0.5)
    quarter_floor = float(os.environ.get("PUBG_PVP_QUARTER_GUN_MIN", "0.038"))
    quarters_active = 0
    for q in range(4):
        q_start = start_sec + q * quarter
        q_dur = quarter if q < 3 else max(duration_sec - 3 * quarter, 0.5)
        density, _, _ = score_pubg_gunfire_audio(video_path, q_start, q_dur)
        if density >= quarter_floor:
            quarters_active += 1
    return clusters, quarters_active, span_ratio


def _ocr_zone_text(frame: np.ndarray, *, y0: float, y1: float, x0: float, x1: float) -> str:
    import cv2

    try:
        import pytesseract
    except ImportError:
        return ""
    small = cv2.resize(frame, (320, 180))
    h, w = small.shape[:2]
    zone = small[int(h * y0) : int(h * y1), int(w * x0) : int(w * x1)]
    gray = cv2.cvtColor(zone, cv2.COLOR_BGR2GRAY)
    gray = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1]
    text = pytesseract.image_to_string(
        gray,
        config="--psm 6 -c tessedit_char_whitelist=ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+- ",
    )
    return " ".join(text.split())


def _pubg_scan_training_ui(
    video_path: Path,
    start_sec: float,
    duration_sec: float,
) -> tuple[bool, str]:
    crop = detect_game_viewport_crop(video_path, start_sec, duration_sec)
    hits: list[str] = []
    for frac in (0.25, 0.5, 0.75):
        frame = _read_frame_at(video_path, start_sec + duration_sec * frac)
        if frame is None:
            continue
        if crop is not None:
            x, y, w, h = crop
            frame = frame[y : y + h, x : x + w]
        banner = _ocr_zone_text(frame, y0=0.0, y1=0.18, x0=0.18, x1=0.82).lower()
        for kw in TRAINING_UI_KEYWORDS:
            if kw in banner:
                hits.append(kw)
    return bool(hits), ",".join(sorted(set(hits))[:3])


def _pubg_killfeed_hits(
    video_path: Path,
    start_sec: float,
    duration_sec: float,
) -> tuple[str, int]:
    crop = detect_game_viewport_crop(video_path, start_sec, duration_sec)
    merged = ""
    best_hits = 0
    for frac in (0.2, 0.5, 0.8):
        frame = _read_frame_at(video_path, start_sec + duration_sec * frac)
        if frame is None:
            continue
        if crop is not None:
            x, y, w, h = crop
            frame = frame[y : y + h, x : x + w]
        text = _ocr_zone_text(frame, y0=0.02, y1=0.22, x0=0.62, x1=0.98)
        merged = f"{merged} {text}".strip()
        hits = sum(1 for kw in KILLFEED_KEYWORDS if kw.lower() in text.lower())
        best_hits = max(best_hits, hits)
    if best_hits == 0 and merged:
        best_hits = sum(1 for kw in KILLFEED_KEYWORDS if kw.lower() in merged.lower())
    return merged[:120], best_hits


def pubg_rejects_bot_farm(
    video_path: Path,
    start_sec: float,
    duration_sec: float,
    *,
    gunfire_density: float,
    center_motion: float = 0.0,
    minimap_delta: float = 0.0,
    ocr_hits: int = 0,
) -> tuple[bool, str, dict[str, Any]]:
    """Reject PUBG PvE/training/bot spray segments that are not real PvP fights."""
    if os.environ.get("PUBG_REJECT_BOT_FARM", "1") != "1":
        return False, "", {}

    out: dict[str, Any] = {}
    try:
        from pubg_owner_calibration import segment_overlaps_owner_label

        if segment_overlaps_owner_label(
            video_path, start_sec, duration_sec, label="bad", pad_sec=10.0
        ):
            return True, "owner_bad_window", out
    except ImportError:
        pass

    training_hit, training_text = _pubg_scan_training_ui(video_path, start_sec, duration_sec)
    out["training_ui"] = training_hit
    if training_hit:
        return True, f"training_mode_ui={training_text}", out

    if ocr_hits <= 0:
        _text, ocr_hits = _pubg_killfeed_hits(video_path, start_sec, duration_sec)
    out["killfeed_hits"] = ocr_hits

    clusters, quarters_active, span_ratio = _gunfire_pvp_shape(video_path, start_sec, duration_sec)
    out["gunfire_clusters"] = clusters
    out["gunfire_quarters_active"] = quarters_active
    out["gunfire_span_ratio"] = round(span_ratio, 3)

    min_quarters = int(os.environ.get("PUBG_PVP_MIN_ACTIVE_QUARTERS", "2"))
    min_clusters = int(os.environ.get("PUBG_PVP_MIN_BURST_CLUSTERS", "2"))
    min_gun = float(os.environ.get("PUBG_BOT_FARM_MIN_GUN", "0.055"))
    min_mini = float(os.environ.get("PUBG_PVP_MIN_MINIMAP", "0.009"))
    min_motion = float(os.environ.get("PUBG_PVP_MIN_CENTER_MOTION", "0.032"))

    if ocr_hits >= 1:
        return False, "", out
    if quarters_active >= min_quarters and clusters >= min_clusters:
        return False, "", out
    if minimap_delta >= min_mini and center_motion >= min_motion:
        return False, "", out
    if clusters >= min_clusters and span_ratio >= float(os.environ.get("PUBG_PVP_MIN_SPAN", "0.35")):
        return False, "", out

    if gunfire_density >= min_gun:
        return (
            True,
            (
                f"bot_farm_one_sided=quarters{quarters_active}:clusters{clusters}:"
                f"kf{ocr_hits}:mini{minimap_delta:.3f}"
            ),
            out,
        )
    return False, "", out


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
    5. PUBG only: NOT bot farm / training / one-sided PvE spray
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

    if profile == "pubg":
        center_motion = float(shoot_row.get("center_motion", 0))
        minimap_delta = 0.0
        if crop is not None:
            _motion, minimap_delta, _skill, _text = score_segment_combat(
                video_path, start_sec, duration_sec, crop_box=crop, sample_frames=5
            )
            center_motion = max(center_motion, _motion)
        ocr_hits = int(getattr(metrics, "ocr_hits", 0) or 0) if metrics is not None else 0
        bot_reject, bot_reason, bot_row = pubg_rejects_bot_farm(
            video_path,
            start_sec,
            duration_sec,
            gunfire_density=gun_density,
            center_motion=center_motion,
            minimap_delta=minimap_delta,
            ocr_hits=ocr_hits,
        )
        out["bot_farm"] = bot_row
        if bot_reject:
            return False, bot_reason, out

    out["pass"] = True
    return True, f"combat_ok=gun{panns_gun:.3f}:burst{shoot_row.get('burst_ratio')}", out
