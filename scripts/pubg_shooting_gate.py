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

# Absolute floors even when soften/env try to lower them (quality floor).
QUALITY_FLOOR_GUNFIRE = 0.055
QUALITY_FLOOR_BURST = 4.8

FORBIDDEN_REASONS = frozenset(
    {
        "run_no_fight",
        "run_no_shots",
        "run_fake_gun",
        "loot_walk",
        "run_loot",
        "talk_menu",
        "talk_low_gun",
        "streamer_talk",
        "no_shots",
        "low_gunfire",
        "silent_segment",
        "owner_bad_window",
    }
)

ALLOWED_OWNER_REASONS = frozenset({"fight_audio", "light_combat", "sniper_hold"})


def _drought_soften_active() -> bool:
    if os.environ.get("VOD_FORCE_SOFTEN", "0") == "1":
        return True
    try:
        return int(os.environ.get("VOD_FORCE_ESCALATION", "0") or 0) > 0
    except ValueError:
        return False


def _min_gunfire() -> float:
    raw = float(os.environ.get("SMART_PUBG_MIN_GUNFIRE_DENSITY", str(MIN_GUNFIRE_DENSITY)))
    # Steady-state keeps the absolute quality floor. Drought soften must be
    # allowed to use VOD_FORCE_GUN_DENSITY / SMART floors or recover stays mute.
    if _drought_soften_active():
        return max(0.0, raw)
    return max(raw, QUALITY_FLOOR_GUNFIRE)


def _min_burst() -> float:
    raw = float(os.environ.get("SMART_PUBG_MIN_BURST_RATIO", str(MIN_BURST_RATIO)))
    if _drought_soften_active():
        return max(0.0, raw)
    return max(raw, QUALITY_FLOOR_BURST)


def reason_is_forbidden(reason: str) -> bool:
    base = reason.split("=", 1)[0].split(":")[0]
    if base in FORBIDDEN_REASONS:
        return True
    return any(fragment in reason for fragment in FORBIDDEN_REASONS)


def owner_reason_counts_as_audio(reason: str) -> bool:
    """Pass reasons from pubg_passes_owner_heuristics (incl. panns_trust=…)."""
    base = reason.split("=", 1)[0].split(":")[0]
    if base in ALLOWED_OWNER_REASONS:
        return True
    # panns_trust / panns_audio / relax_* / tiktok_* are explicit pass tokens.
    return base.startswith(("panns_", "relax_", "tiktok_"))


def pubg_probe_segment(
    video_path: Path,
    start_sec: float,
    duration_sec: float,
    *,
    crop_box: tuple[int, int, int, int] | None = None,
) -> dict:
    """Collect gunfire/motion metrics for logging and gate checks."""
    if crop_box is None:
        if os.environ.get("VOD_VIEWPORT_CACHE", "1") == "1":
            from vod_viewport_cache import detect_viewport_cached

            crop_box = detect_viewport_cached(video_path, start_sec, duration_sec)
        else:
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
        from pubg_owner_calibration import owner_bad_pad_sec, segment_overlaps_owner_label

        if segment_overlaps_owner_label(
            video_path, start_sec, duration_sec, label="bad", pad_sec=owner_bad_pad_sec()
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
    # ok_owner already means heuristics passed; do not drop panns_trust just because
    # the reason string is not exactly fight_audio/light_combat (that blocked montages).
    heuristic_audio = bool(ok_owner) and owner_reason_counts_as_audio(owner_reason)

    if owner_reason == "sniper_hold" or owner_reason.startswith("sniper_hold"):
        if motion < 0.030:
            return False, f"sniper_hold_no_motion=motion{motion:.3f}:gun{gun:.3f}", metrics
        if gun < min_gun * 0.90:
            return False, f"sniper_hold_weak=gun{gun:.3f}", metrics

    if not strict_audio and not heuristic_audio:
        return (
            False,
            f"no_shots=density{gun:.3f}:burst{burst:.2f}:owner{owner_reason}",
            metrics,
        )

    crop = tuple(metrics["crop_box"]) if metrics.get("crop_box") else crop_box
    if crop is not None:
        crop = tuple(int(v) for v in crop)

    if segment_looks_like_pubg_loot_or_walk(
        video_path,
        start_sec,
        duration_sec,
        crop_box=crop,
        gunfire_density=gun,
        burst_ratio=burst,
    ):
        # Strong PANNs gunfire: don't call continuous auto-fire "loot".
        # Spike-density alone under-counts sprays; require audible energy too.
        # Floor must match owner trust (0.40) — 0.28 let ambient/UI SFX override.
        panns_floor = float(
            os.environ.get(
                "PUBG_PANNS_LOOT_OVERRIDE_MIN",
                os.environ.get("PUBG_PANNS_TRUST_QUALITY_FLOOR", "0.40"),
            )
        )
        panns_strong = panns_gun_max >= panns_floor
        audible = gun >= min_gun * 0.85 or (rms >= 0.035 and gun >= min_gun * 0.55)
        if panns_strong and audible:
            metrics["panns_loot_override"] = True
        elif not (panns_gun_max >= 0.45 and gun >= min_gun * 0.85):
            return False, f"loot_walk=density{gun:.3f}", metrics

    gate_ok, gate_reason = segment_is_valid_for_montage(
        video_path,
        start_sec,
        duration_sec,
        profile="pubg",
        crop_box=crop,
        min_gunfire=min_gun,
    )
    metrics["gate_reason"] = gate_reason

    if reason_is_forbidden(gate_reason):
        base = str(gate_reason).split("=", 1)[0]
        try:
            from pubg_owner_calibration import segment_overlaps_owner_label

            owner_good = segment_overlaps_owner_label(
                video_path, start_sec, duration_sec, label="good", pad_sec=10.0
            )
        except ImportError:
            owner_good = False
        # Soften must not auto-forgive weak loot UI as "strict_audio"
        # (_-HbZ0zNDOs_2538). Allow overrides only when:
        # - owner 👍 on this window, or
        # - owner heuristics already returned panns_trust AND DSP gun is above
        #   the fake-gun ceiling (ADS sprays with aim sway, gun~0.06–0.10).
        if base in {"run_no_fight", "run_fake_gun", "run_no_shots", "run_loot", "loot_walk"}:
            hard_gun = float(os.environ.get("PUBG_FAKE_GUN_OVERRIDE_MIN_GUN", "0.090"))
            fake_gun_ceil = float(os.environ.get("PUBG_PANNS_FAKE_GUN_MAX", "0.060"))
            panns_floor = float(os.environ.get("PUBG_PANNS_TRUST_MIN", "0.35"))
            owner_reason = str(metrics.get("owner_reason") or "")
            panns_trusted = owner_reason.startswith("panns_trust") and panns_gun_max >= panns_floor
            if owner_good and gun >= hard_gun and burst >= min_burst:
                metrics["visual_override"] = gate_reason
            elif panns_trusted and gun >= fake_gun_ceil and (
                burst >= min_burst * 0.75 or gun >= min_gun
            ):
                metrics["panns_visual_override"] = gate_reason
            else:
                return False, gate_reason, metrics
        else:
            return False, gate_reason, metrics

    if not gate_ok and not (
        metrics.get("visual_override") or metrics.get("panns_visual_override")
    ):
        return False, gate_reason, metrics

    pass_reason = (
        f"strict_gun=gun{gun:.3f}:burst{burst:.2f}"
        if strict_audio
        else f"{owner_reason}=gun{gun:.3f}:burst{burst:.2f}"
    )
    if metrics.get("visual_override"):
        pass_reason = f"{pass_reason}+override:{metrics['visual_override'].split('=')[0]}"
    if metrics.get("panns_visual_override"):
        pass_reason = (
            f"{pass_reason}+panns_override:{metrics['panns_visual_override'].split('=')[0]}"
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
