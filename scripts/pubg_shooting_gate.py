#!/usr/bin/env python3
"""Unified PUBG montage gate: every segment must contain audible gunfire (no run/loot/talk)."""

from __future__ import annotations

import os
from pathlib import Path

from gameplay_gate import (
    detect_game_viewport_crop,
    score_pubg_gunfire_audio,
    score_segment_combat,
    segment_is_valid_for_montage,
    segment_looks_like_pubg_loot_or_walk,
)

MIN_GUNFIRE_DENSITY = 0.068
MIN_BURST_RATIO = 5.2

FORBIDDEN_REASONS = frozenset(
    {
        "run_no_fight",
        "run_no_shots",
        "run_fake_gun",
        "loot_walk",
        "run_loot",
        "talk_menu",
        "talk_low_gun",
        "no_shots",
        "low_gunfire",
        "silent_segment",
        "owner_bad_window",
    }
)

ALLOWED_OWNER_REASONS = frozenset({"fight_audio", "light_combat"})
# Prefixes returned by pubg_passes_owner_heuristics when PANNs/relax says combat.
ALLOWED_OWNER_PREFIXES = (
    "fight_audio",
    "light_combat",
    "panns_trust",
    "panns_audio",
    "panns_relax",
    "relax_fight",
)


def _owner_reason_allows_pass(owner_reason: str) -> bool:
    base = (owner_reason or "").split("=", 1)[0].strip()
    if base in ALLOWED_OWNER_REASONS:
        return True
    return any(base.startswith(p) for p in ALLOWED_OWNER_PREFIXES)


def _min_gunfire() -> float:
    return float(os.environ.get("SMART_PUBG_MIN_GUNFIRE_DENSITY", str(MIN_GUNFIRE_DENSITY)))


def _min_burst() -> float:
    return float(os.environ.get("SMART_PUBG_MIN_BURST_RATIO", str(MIN_BURST_RATIO)))


def reason_is_forbidden(reason: str) -> bool:
    base = reason.split("=", 1)[0].split(":")[0]
    if base in FORBIDDEN_REASONS:
        return True
    return any(fragment in reason for fragment in FORBIDDEN_REASONS)


def pubg_probe_segment(
    video_path: Path,
    start_sec: float,
    duration_sec: float,
    *,
    crop_box: tuple[int, int, int, int] | None = None,
) -> dict:
    """Collect gunfire/motion metrics for logging and gate checks."""
    if crop_box is None:
        crop_box = detect_game_viewport_crop(video_path, start_sec, duration_sec)
    gun, burst, rms = score_pubg_gunfire_audio(video_path, start_sec, duration_sec)
    motion, _mini, _skill, center_text = score_segment_combat(
        video_path, start_sec, duration_sec, crop_box=crop_box, sample_frames=5
    )
    return {
        "start": round(start_sec, 3),
        "duration": round(duration_sec, 3),
        "gunfire_density": round(gun, 4),
        "burst_ratio": round(burst, 3),
        "audio_rms": round(rms, 4),
        "center_motion": round(motion, 4),
        "center_text": round(center_text, 3),
        "crop_box": list(crop_box) if crop_box else None,
    }


def pubg_passes_shooting_gate(
    video_path: Path,
    start_sec: float,
    duration_sec: float,
    *,
    crop_box: tuple[int, int, int, int] | None = None,
    panns_gun_max: float = 0.0,
) -> tuple[bool, str, dict]:
    """
    MUST for every PUBG montage segment.

    Pass if gun >= 0.055 and burst >= 4.8, or owner heuristics fight_audio/light_combat.
    Reject sniper_hold without visible aim motion, and all forbidden gate reasons.
    """
    try:
        from pubg_owner_calibration import segment_overlaps_owner_label

        if segment_overlaps_owner_label(
            video_path, start_sec, duration_sec, label="bad", pad_sec=14.0
        ):
            metrics = pubg_probe_segment(video_path, start_sec, duration_sec, crop_box=crop_box)
            return False, "owner_bad_window", metrics
    except ImportError:
        pass

    metrics = pubg_probe_segment(video_path, start_sec, duration_sec, crop_box=crop_box)
    gun = float(metrics["gunfire_density"])
    burst = float(metrics["burst_ratio"])
    rms = float(metrics["audio_rms"])
    motion = float(metrics["center_motion"])
    min_gun = _min_gunfire()
    min_burst = _min_burst()

    try:
        from pubg_owner_calibration import pubg_passes_owner_heuristics
    except ImportError:
        from pubg_owner_calibration import pubg_passes_owner_heuristics  # type: ignore

    ok_owner, owner_reason = pubg_passes_owner_heuristics(
        gun, burst, rms, motion, panns_gun_max=panns_gun_max
    )
    metrics["owner_reason"] = owner_reason
    metrics["panns_gun_max"] = round(float(panns_gun_max), 4)

    if reason_is_forbidden(owner_reason) and panns_gun_max < float(
        os.environ.get("PUBG_PANNS_TRUST_MIN", "0.35")
    ):
        return False, owner_reason, metrics

    strict_audio = gun >= min_gun and burst >= min_burst
    # PANNs trust must count — otherwise gunfire heard at 0.5–0.7 still dies as
    # no_shots=...:ownerpanns_trust (legacy whitelist only had fight_audio/light_combat).
    heuristic_audio = ok_owner and _owner_reason_allows_pass(owner_reason)

    if owner_reason == "sniper_hold" or owner_reason.startswith("sniper_hold"):
        if motion < 0.030:
            return False, f"sniper_hold_no_motion=motion{motion:.3f}:gun{gun:.3f}", metrics
        if gun < min_gun * 0.90:
            return False, f"sniper_hold_weak=gun{gun:.3f}", metrics
        heuristic_audio = heuristic_audio or (ok_owner and motion >= 0.030 and gun >= min_gun * 0.90)

    if not strict_audio and not heuristic_audio:
        return (
            False,
            f"no_shots=density{gun:.3f}:burst{burst:.2f}:owner{owner_reason}",
            metrics,
        )

    crop = tuple(metrics["crop_box"]) if metrics.get("crop_box") else crop_box
    if crop is not None:
        crop = tuple(int(v) for v in crop)

    panns_trusted = float(panns_gun_max or 0.0) >= float(
        os.environ.get("PUBG_PANNS_TRUST_MIN", "0.35")
    )

    # Density-only loot_walk false-positives on real Metro gunfights with low
    # energy metric but strong PANNs (0.5–0.7). Trust PANNs over walk heuristic.
    if (
        not panns_trusted
        and segment_looks_like_pubg_loot_or_walk(
            video_path,
            start_sec,
            duration_sec,
            crop_box=crop,
            gunfire_density=gun,
        )
    ):
        return False, f"loot_walk=density{gun:.3f}", metrics

    gate_ok, gate_reason = segment_is_valid_for_montage(
        video_path,
        start_sec,
        duration_sec,
        profile="pubg",
        crop_box=crop,
        min_gunfire=min_gun,
        panns_gun_max=float(panns_gun_max or 0.0),
    )
    metrics["gate_reason"] = gate_reason

    # Montage used to re-run owner heuristics without PANNs and convert trusted
    # gunfights into run_fake_gun / no_shots. Keep hard junk rejects only.
    soft_motion_reject = gate_reason.split("=", 1)[0] in {
        "run_fake_gun",
        "run_loot",
        "run_no_fight",
        "run_no_shots",
        "no_shots",
        "below_owner_floor",
        "streamer_talk",
        "loot_walk",
    }
    if reason_is_forbidden(gate_reason) and not (panns_trusted and soft_motion_reject):
        return False, gate_reason, metrics

    if not gate_ok and not (panns_trusted and soft_motion_reject):
        return False, gate_reason, metrics

    pass_reason = (
        f"strict_gun=gun{gun:.3f}:burst{burst:.2f}"
        if strict_audio
        else f"{owner_reason}=gun{gun:.3f}:burst{burst:.2f}"
    )
    return True, pass_reason, metrics


def format_segment_metrics_line(metrics: dict, reason: str, *, ok: bool) -> str:
    status = "PASS" if ok else "FAIL"
    return (
        f"[{status}] start={metrics.get('start')} "
        f"gun={metrics.get('gunfire_density')} burst={metrics.get('burst_ratio')} "
        f"motion={metrics.get('center_motion')} rms={metrics.get('audio_rms')} "
        f"reason={reason}"
    )


def verify_montage_segments(
    video_path: Path,
    segments: list[tuple[float, float]],
) -> tuple[bool, list[dict], list[str]]:
    """Verify every (start, duration) before send. Returns (all_ok, metrics_list, log_lines)."""
    all_ok = True
    metrics_list: list[dict] = []
    log_lines: list[str] = []
    for start, duration in segments:
        ok, reason, metrics = pubg_passes_shooting_gate(video_path, start, duration)
        metrics["pass"] = ok
        metrics["reason"] = reason
        metrics_list.append(metrics)
        line = format_segment_metrics_line(metrics, reason, ok=ok)
        log_lines.append(line)
        if not ok:
            all_ok = False
    return all_ok, metrics_list, log_lines
