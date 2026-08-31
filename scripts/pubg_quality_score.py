#!/usr/bin/env python3
"""PUBG presend quality fusion: hard-reject junk, score ambiguous signals."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any


def _clip(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, float(value)))


def _owner_bad(video_path: Path, start_sec: float, duration_sec: float) -> bool:
    if os.environ.get("PUBG_OWNER_BAD_HARD_REJECT", "1") != "1":
        return False
    try:
        from pubg_owner_calibration import segment_overlaps_owner_label

        return bool(
            segment_overlaps_owner_label(
                video_path,
                start_sec,
                duration_sec,
                label="bad",
                pad_sec=8.0,
            )
        )
    except Exception:
        return False


def score_pubg_window(
    video_path: Path,
    start_sec: float,
    duration_sec: float,
) -> tuple[bool, str, dict[str, Any]]:
    """Return acceptance, reason and complete feature/penalty report."""
    from highlight_scorer import score_panns_audio
    from pubg_combat_gate import _pubg_scan_training_ui, pubg_combat_visual_strict
    from pubg_killfeed_ocr import score_killfeed_segment
    from pubg_shooting_gate import pubg_probe_segment
    from shooter_author_kill_gate import detect_author_death_signals

    report: dict[str, Any] = {
        "start": round(float(start_sec), 3),
        "duration": round(float(duration_sec), 3),
        "score_mode": True,
    }
    if _owner_bad(video_path, start_sec, duration_sec):
        report["hard_reject"] = "owner_bad_window"
        return False, "hard_owner_bad_window", report

    shoot = pubg_probe_segment(video_path, start_sec, duration_sec)
    report.update(shoot)
    panns = score_panns_audio(video_path, start_sec, duration_sec)
    report.update({key: round(float(value), 4) for key, value in panns.items()})

    gun = float(shoot.get("gunfire_density", 0.0))
    burst = float(shoot.get("burst_ratio", 0.0))
    rms = float(shoot.get("audio_rms", 0.0))
    motion = float(shoot.get("center_motion", 0.0))
    panns_gun = float(panns.get("panns_gun_max", 0.0))

    center_text = float(shoot.get("center_text", 0.0))
    loot_walk = (
        (motion >= 0.030 and gun < 0.040)
        or (motion < 0.014 and gun < 0.028)
        or (center_text > 0.14 and gun < 0.040)
    )
    gate_ok = not loot_walk
    gate_reason = "loot_walk" if loot_walk else "score_features_ok"
    report["loot_walk"] = bool(loot_walk)
    report["legacy_gate_ok"] = bool(gate_ok)
    report["legacy_gate_reason"] = gate_reason

    visual_ok, visual_reason, visual = pubg_combat_visual_strict(
        video_path,
        start_sec,
        duration_sec,
        "pubg",
    )
    report["visual_ok"] = visual_ok
    report["visual_reason"] = visual_reason
    report["visual"] = visual

    try:
        killfeed, killfeed_row = score_killfeed_segment(
            video_path, start_sec, duration_sec, "pubg"
        )
    except Exception:
        killfeed, killfeed_row = 0.0, {}
    report["killfeed_density"] = round(float(killfeed), 4)
    report["killfeed"] = killfeed_row

    best_flash = float(visual.get("best_hit_flash", 0.0))
    best_weapon = float(visual.get("best_weapon_edge", 0.0))
    has_kill = (
        float(killfeed) >= 0.20
        or best_flash >= float(os.environ.get("SHOOTER_AUTHOR_KILL_MIN_HIT_FLASH", "0.004"))
        or (
            best_weapon >= float(os.environ.get("SHOOTER_AUTHOR_KILL_MIN_WEAPON_EDGE", "0.030"))
            and gun >= 0.055
            and motion >= 0.030
        )
    )
    author_death = False
    author_reason = "author_kill_signal" if has_kill else "no_author_kill"
    author: dict[str, Any] = {
        "has_author_kill": has_kill,
        "author_death": False,
        "killfeed_density": float(killfeed),
        "hit_flash": best_flash,
        "weapon_edge": best_weapon,
    }
    # Death OCR is expensive. Run it only when no positive payoff signal exists.
    if not has_kill:
        author_death, death_reason, death_metrics = detect_author_death_signals(
            video_path,
            start_sec,
            duration_sec,
        )
        author["author_death"] = author_death
        author["death_metrics"] = death_metrics
        if author_death:
            author_reason = death_reason or "author_death"
    report["author_ok"] = has_kill or not author_death
    report["author_reason"] = author_reason
    report["author"] = author
    if author_death and not has_kill:
        report["hard_reject"] = "author_death"
        return False, f"hard_{author_reason or 'author_death'}", report

    # Near-silent/no-action windows are content junk, not an ambiguous quality miss.
    if gun < 0.010 and panns_gun < 0.08 and rms < 0.012:
        report["hard_reject"] = "no_action"
        return False, "hard_no_action", report
    # OCR training/menu detection is reserved for suspicious text-heavy,
    # low-action windows instead of adding three OCR reads to every candidate.
    if center_text > 0.18 and gun < 0.030 and panns_gun < 0.18:
        training, training_text = _pubg_scan_training_ui(video_path, start_sec, duration_sec)
        if training:
            report["training_ui"] = training_text
            report["hard_reject"] = "training_ui"
            return False, f"hard_training_ui={training_text}", report

    components = {
        "panns": _clip(panns_gun / 0.45) * 0.20,
        "gun": _clip(gun / 0.080) * 0.16,
        "burst": _clip(burst / 8.0) * 0.08,
        "motion": _clip(motion / 0.060) * 0.10,
        "killfeed": _clip(float(killfeed)) * 0.14,
        "author_kill": (0.18 if has_kill else 0.0),
        "visual": (0.09 if visual_ok else 0.0),
        "audio_presence": _clip(rms / 0.050) * 0.05,
    }
    penalties = {
        "loot_walk": 0.16 if loot_walk else 0.0,
        "no_author_kill": 0.12 if not has_kill else 0.0,
        "visual_fail": 0.08 if not visual_ok else 0.0,
        "legacy_gate": 0.06 if not gate_ok else 0.0,
        "speech_music": _clip(
            max(float(panns.get("panns_speech", 0.0)), float(panns.get("panns_music", 0.0)))
            - panns_gun
        )
        * 0.08,
    }
    heuristic = _clip(sum(components.values()) - sum(penalties.values()))

    ranker_score = None
    try:
        from pubg_moment_ranker import predict_from_features

        ranker_score = predict_from_features(
            {
                **{key: float(value) for key, value in panns.items()},
                **{key: float(shoot.get(key, 0.0)) for key in (
                    "gunfire_density",
                    "burst_ratio",
                    "audio_rms",
                    "center_motion",
                )},
                "killfeed_density": float(killfeed),
            }
        )
    except Exception:
        ranker_score = None
    blend = float(os.environ.get("PUBG_QUALITY_RANKER_WEIGHT", "0.30"))
    quality = heuristic
    if ranker_score is not None:
        quality = _clip(heuristic * (1.0 - blend) + float(ranker_score) * blend)

    threshold = float(os.environ.get("PUBG_QUALITY_SCORE_MIN", "0.48"))
    report.update(
        {
            "components": {key: round(value, 4) for key, value in components.items()},
            "penalties": {key: round(value, 4) for key, value in penalties.items()},
            "heuristic_score": round(heuristic, 4),
            "ranker_score": None if ranker_score is None else round(float(ranker_score), 4),
            "quality_score": round(quality, 4),
            "quality_threshold": threshold,
            "has_author_kill": has_kill,
        }
    )
    if quality < threshold:
        return False, f"quality_low={quality:.3f}:min{threshold:.2f}", report
    return True, f"quality_ok={quality:.3f}", report


__all__ = ["score_pubg_window"]
