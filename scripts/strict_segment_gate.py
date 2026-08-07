#!/usr/bin/env python3
"""Strict peak-moment gate for all 5 game profiles — no filler montages."""

from __future__ import annotations

import os
from pathlib import Path

from gameplay_gate import (
    detect_game_viewport_crop,
    score_genshin_boss_likelihood,
    score_pubg_gunfire_audio,
    score_segment_combat,
    score_segment_window,
    segment_is_valid_for_montage,
)

PROFILE_ALIASES = {
    "mlbb": "mobile_legends",
    "world_of_tanks": "wot",
}

GAME_LABELS = {
    "mobile_legends": "MLBB",
    "pubg": "PUBG",
    "genshin": "Genshin",
    "standoff": "Standoff",
    "wot": "WoT",
}


def normalize_profile(profile: str) -> str:
    p = profile.strip().lower()
    return PROFILE_ALIASES.get(p, p)


def probe_segment(
    video_path: Path,
    start_sec: float,
    duration_sec: float,
    profile: str,
    *,
    crop_box: tuple[int, int, int, int] | None = None,
) -> dict:
    profile = normalize_profile(profile)
    if crop_box is None:
        crop_box = detect_game_viewport_crop(video_path, start_sec, duration_sec)

    metrics: dict = {
        "start": round(start_sec, 3),
        "duration": round(duration_sec, 3),
        "profile": profile,
    }

    if profile in ("pubg", "standoff", "wot"):
        gun, burst, rms = score_pubg_gunfire_audio(video_path, start_sec, duration_sec)
        motion, mini, skill, center_text = score_segment_combat(
            video_path, start_sec, duration_sec, crop_box=crop_box, sample_frames=6
        )
        metrics.update(
            {
                "gunfire_density": round(gun, 4),
                "impact_density": round(gun, 4),
                "burst_ratio": round(burst, 3),
                "audio_rms": round(rms, 4),
                "center_motion": round(motion, 4),
                "center_text": round(center_text, 3),
            }
        )
    elif profile == "genshin":
        bar, motion, bscore, peak = score_genshin_boss_likelihood(
            video_path, start_sec, duration_sec, crop_box=crop_box
        )
        metrics.update(
            {
                "boss_bar": round(bar, 4),
                "bar_peak": round(peak, 4),
                "boss_score": round(bscore, 4),
                "center_motion": round(motion, 4),
            }
        )
    else:
        hud, text, cartoon = score_segment_window(
            video_path, start_sec, duration_sec, crop_box=crop_box
        )
        motion, mini, skill, center_text = score_segment_combat(
            video_path, start_sec, duration_sec, crop_box=crop_box, sample_frames=6
        )
        metrics.update(
            {
                "hud": round(hud, 2),
                "overlay_text": round(text, 4),
                "center_motion": round(motion, 4),
                "minimap_delta": round(mini, 4),
                "skill_delta": round(skill, 4),
                "center_text": round(center_text, 3),
            }
        )
    return metrics


def _wot_extra_reject(metrics: dict) -> tuple[bool, str]:
    """Reject tank cruise: motion without sustained hits."""
    soft = os.environ.get("SHOOTER_VOD_MONTAGE_SOFT_GATE", "0") == "1"
    impact = float(metrics.get("impact_density", 0))
    motion = float(metrics.get("center_motion", 0))
    flashes = int(metrics.get("hit_flash_count", 0))
    burst = float(metrics.get("burst_ratio", 0))
    min_impact = float(os.environ.get("SMART_WOT_MIN_IMPACT_DENSITY", "0.052"))
    if soft:
        min_impact = min(min_impact, float(os.environ.get("WOT_SOFT_MIN_IMPACT_DENSITY", "0.008")))
    # cruise_cap = minimum impact required while moving (name is historical).
    cruise_cap = float(
        os.environ.get(
            "WOT_BRAWL_CRUISE_IMPACT_MAX",
            str(max(min_impact * 1.35, 0.070)),
        )
    )
    if soft:
        # Soft/SLA must LOWER the moving-impact floor. Using max(..., 0.10/0.15)
        # previously rejected every real WoT fight (typical impact 0.015–0.04).
        soft_cruise = float(os.environ.get("WOT_SOFT_CRUISE_IMPACT_MAX", "0.015"))
        if os.environ.get("SMART_WOT_CRUISE_IMPACT_CAP"):
            soft_cruise = min(soft_cruise, float(os.environ["SMART_WOT_CRUISE_IMPACT_CAP"]))
        cruise_cap = min(cruise_cap, soft_cruise)
    if os.environ.get("WOT_BRAWL_GATE", "1") == "1":
        min_flashes = max(1, int(os.environ.get("WOT_BRAWL_MIN_HIT_FLASHES", "2")))
        if soft:
            min_flashes = max(1, int(os.environ.get("WOT_SOFT_MIN_HIT_FLASHES", "1")))
        if flashes and flashes < min_flashes:
            return True, f"low_hit_flashes={flashes}:need{min_flashes}"
        if impact < min_impact:
            return True, f"no_hits=density{impact:.3f}"
        if motion >= 0.10 and impact < cruise_cap:
            return True, f"cruise_no_action=motion{motion:.3f}:impact{impact:.3f}"
        if impact < min_impact * 1.05 and burst < float(os.environ.get("SMART_WOT_MIN_BURST_RATIO", "2.3")):
            return True, f"empty_drive=density{impact:.3f}:burst{burst:.2f}"
        return False, ""
    if impact < min_impact:
        return True, f"no_hits=density{impact:.3f}"
    if motion >= 0.10 and impact < cruise_cap:
        return True, f"cruise_no_action=motion{motion:.3f}:impact{impact:.3f}"
    if impact < min_impact * 1.05 and burst < float(os.environ.get("SMART_WOT_MIN_BURST_RATIO", "2.3")):
        return True, f"empty_drive=density{impact:.3f}:burst{burst:.2f}"
    return False, ""


def _standoff_extra_reject(metrics: dict) -> tuple[bool, str]:
    """Reject run/idle rounds: need sustained gunfire + aim motion."""
    gun = float(metrics.get("gunfire_density", 0))
    burst = float(metrics.get("burst_ratio", 0))
    motion = float(metrics.get("center_motion", 0))
    min_gun = float(os.environ.get("SMART_STANDOFF_MIN_GUNFIRE_DENSITY", "0.10"))
    min_burst = float(os.environ.get("SMART_STANDOFF_MIN_BURST_RATIO", "8.0"))
    min_motion = float(os.environ.get("SMART_STANDOFF_MIN_CENTER_MOTION", "0.12"))
    if gun < min_gun:
        return True, f"low_gunfire=density{gun:.3f}:need{min_gun:.2f}"
    if burst < min_burst:
        return True, f"low_burst=burst{burst:.2f}:need{min_burst:.1f}"
    if motion < min_motion:
        return True, f"run_no_fight=motion{motion:.3f}:gun{gun:.3f}"
    return False, ""


def _genshin_extra_reject(metrics: dict) -> tuple[bool, str]:
    """Stricter boss fight bar on top of gameplay_gate boss_ok."""
    motion = float(metrics.get("center_motion", 0))
    boss_score = float(metrics.get("boss_score", 0))
    min_motion = float(os.environ.get("SMART_GENSHIN_STRICT_MIN_CENTER_MOTION", "0.18"))
    min_score = float(os.environ.get("SMART_GENSHIN_STRICT_MIN_BOSS_SCORE", "0.35"))
    if motion < min_motion:
        return True, f"low_boss_motion=motion{motion:.3f}:need{min_motion:.2f}"
    if boss_score < min_score:
        return True, f"weak_boss_score=score{boss_score:.2f}:need{min_score:.2f}"
    return False, ""


def _mlbb_extra_reject(metrics: dict) -> tuple[bool, str]:
    """Reject lane walk / low fight activity."""
    motion = float(metrics.get("center_motion", 0))
    mini = float(metrics.get("minimap_delta", 0))
    skill = float(metrics.get("skill_delta", 0))
    text = float(metrics.get("overlay_text", 0))
    max_text = float(os.environ.get("SMART_MAX_OVERLAY_TEXT", "0.10"))
    if text > max_text:
        return True, f"overlay_text={text:.3f}"
    min_fight_motion = float(os.environ.get("SMART_MLBB_MIN_FIGHT_MOTION", "0.042"))
    if motion < min_fight_motion and mini < 0.013:
        return True, f"lane_walk=motion{motion:.3f}:mini{mini:.3f}"
    if skill < 0.007 and motion < min_fight_motion * 1.1:
        return True, f"no_fight_activity=skill{skill:.3f}"
    return False, ""


def passes_strict_gate(
    video_path: Path,
    start_sec: float,
    duration_sec: float,
    profile: str,
    *,
    crop_box: tuple[int, int, int, int] | None = None,
) -> tuple[bool, str, dict]:
    profile = normalize_profile(profile)
    metrics = probe_segment(video_path, start_sec, duration_sec, profile, crop_box=crop_box)

    if profile == "pubg":
        from pubg_combat_gate import pubg_passes_combat_gate

        ok, reason, pubg_metrics = pubg_passes_combat_gate(
            video_path, start_sec, duration_sec, "pubg"
        )
        metrics.update(pubg_metrics)
        metrics["gate_reason"] = reason
        return ok, reason, metrics

    gate_profile = "world_of_tanks" if profile == "wot" else profile
    crop = crop_box
    if metrics.get("crop_box"):
        crop = tuple(int(v) for v in metrics["crop_box"])

    ok, gate_reason = segment_is_valid_for_montage(
        video_path,
        start_sec,
        duration_sec,
        profile=gate_profile,
        crop_box=crop,
    )
    metrics["gate_reason"] = gate_reason

    if not ok:
        return False, gate_reason, metrics

    if profile == "wot":
        from wot_brawl_segment import validate_wot_brawl_segment

        brawl_ok, brawl_reason, brawl_metrics = validate_wot_brawl_segment(
            video_path, start_sec, duration_sec, metrics=metrics
        )
        metrics.update(brawl_metrics)
        if not brawl_ok:
            metrics["gate_reason"] = brawl_reason
            return False, brawl_reason, metrics
        bad, extra = _wot_extra_reject(metrics)
        if bad:
            metrics["gate_reason"] = extra
            return False, extra, metrics

    if profile == "mobile_legends":
        bad, extra = _mlbb_extra_reject(metrics)
        if bad:
            metrics["gate_reason"] = extra
            return False, extra, metrics

    if profile == "genshin":
        bad, extra = _genshin_extra_reject(metrics)
        if bad:
            metrics["gate_reason"] = extra
            return False, extra, metrics

    if profile == "standoff":
        bad, extra = _standoff_extra_reject(metrics)
        if bad:
            metrics["gate_reason"] = extra
            return False, extra, metrics

    return True, gate_reason, metrics


def key_metrics_summary(profile: str, metrics: dict) -> str:
    profile = normalize_profile(profile)
    if profile == "mobile_legends":
        return (
            f"hud={metrics.get('hud')} motion={metrics.get('center_motion')} "
            f"text={metrics.get('overlay_text')}"
        )
    if profile == "genshin":
        return (
            f"boss_bar={metrics.get('boss_bar')} peak={metrics.get('bar_peak')} "
            f"score={metrics.get('boss_score')} motion={metrics.get('center_motion')}"
        )
    if profile in ("pubg", "standoff", "wot"):
        key = "gunfire_density" if profile != "wot" else "impact_density"
        return (
            f"{key.split('_')[0]}={metrics.get(key) or metrics.get('gunfire_density')} "
            f"burst={metrics.get('burst_ratio')} motion={metrics.get('center_motion')} "
            f"rms={metrics.get('audio_rms')}"
        )
    return str(metrics)


def format_acceptance_table(game: str, rows: list[dict]) -> str:
    profile = rows[0].get("profile", "") if rows else ""
    lines = [
        f"ACCEPTANCE game={game} profile={profile}",
        "| # | start | key_metrics | gate_reason | pass |",
        "|---|-------|-------------|-------------|------|",
    ]
    for idx, row in enumerate(rows, start=1):
        status = "PASS" if row.get("pass") else "FAIL"
        lines.append(
            f"| {idx} | {row.get('start')} | {key_metrics_summary(profile, row)} | "
            f"{row.get('gate_reason', row.get('reason', ''))} | {status} |"
        )
    all_ok = all(row.get("pass") for row in rows)
    lines.append(f"ALL_PASS={all_ok} segments={len(rows)}")
    return "\n".join(lines)


def verify_montage_segments(
    video_path: Path,
    profile: str,
    segments: list[tuple[float, float]],
) -> tuple[bool, list[dict], str]:
    """Verify every segment; returns (all_ok, metrics_rows, acceptance_table)."""
    rows: list[dict] = []
    all_ok = True
    for start, duration in segments:
        ok, reason, metrics = passes_strict_gate(video_path, start, duration, profile)
        metrics["pass"] = ok
        metrics["reason"] = reason
        if not ok:
            all_ok = False
        rows.append(metrics)

    game = GAME_LABELS.get(normalize_profile(profile), profile)
    table = format_acceptance_table(game, rows)
    return all_ok, rows, table
