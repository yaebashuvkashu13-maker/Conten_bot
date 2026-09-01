#!/usr/bin/env python3
"""Fast peak scoring for rank stage — notification + audio, no visual/OCR death."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any


def score_peak_fast(
    video_path: Path,
    peak_sec: float,
    *,
    part_sec: float = 14.0,
    profile: str = "pubg",
) -> dict[str, Any]:
    """Cheap peak score used before presend; prioritizes kill notification."""
    from highlight_scorer import score_panns_audio
    from pubg_shooting_gate import pubg_probe_segment

    start = max(0.0, float(peak_sec) - float(part_sec) * 0.5)
    dur = float(part_sec)
    shoot = pubg_probe_segment(video_path, start, dur)
    panns = score_panns_audio(video_path, start, dur)
    gun = float(shoot.get("gunfire_density", 0.0))
    motion = float(shoot.get("center_motion", 0.0))
    panns_gun = float(panns.get("panns_gun_max", 0.0))

    notification_score = 0.0
    notification_hit = False
    killfeed = 0.0
    if profile == "pubg" and os.environ.get("PUBG_KILL_NOTIFICATION_ENABLED", "1") == "1":
        try:
            from pubg_kill_notification import score_kill_notification_segment

            notification_score, nmeta = score_kill_notification_segment(video_path, start, dur)
            notification_hit = float(notification_score) >= float(
                os.environ.get("PUBG_KILL_NOTIFICATION_MIN_SCORE", "0.50")
            )
            killfeed = float(nmeta.get("notification_score", notification_score) or 0.0)
        except Exception:
            pass
    elif profile in ("pubg", "standoff"):
        try:
            from pubg_killfeed_ocr import score_killfeed_segment

            killfeed, kmeta = score_killfeed_segment(video_path, start, dur, profile)
            notification_score = float(kmeta.get("notification_score", 0.0) or 0.0)
            notification_hit = notification_score >= float(
                os.environ.get("PUBG_KILL_NOTIFICATION_MIN_SCORE", "0.50")
            )
        except Exception:
            pass

    fight = min(1.0, gun / 0.080) * 0.35 + min(1.0, panns_gun / 0.45) * 0.25 + min(1.0, motion / 0.06) * 0.20
    payoff = min(1.0, float(notification_score)) * 0.55 + min(1.0, float(killfeed)) * 0.25
    if notification_hit:
        payoff += 0.20
    loot_walk = (motion >= 0.030 and gun < 0.040) or (motion < 0.014 and gun < 0.028)
    composite = fight * 0.45 + payoff * 0.55
    if loot_walk:
        composite *= 0.55
    if gun < 0.010 and panns_gun < 0.08:
        composite *= 0.25

    return {
        "peak_sec": round(float(peak_sec), 2),
        "start": round(start, 2),
        "fight_fast": round(fight, 4),
        "payoff_fast": round(payoff, 4),
        "fast_score": round(composite, 4),
        "notification_score": round(float(notification_score), 4),
        "notification_hit": notification_hit,
        "killfeed": round(float(killfeed), 4),
        "gunfire_density": round(gun, 4),
        "panns_gun_max": round(panns_gun, 4),
        "loot_walk": loot_walk,
    }


def rank_peaks_fast(
    video_path: Path,
    peaks: list[float],
    profile: str,
    *,
    part_sec: float = 14.0,
    max_probes: int | None = None,
) -> tuple[list[float], str, dict[float, dict[str, Any]]]:
    """Reorder peaks by fast fight+payoff; deprioritize no-notification peaks."""
    if not peaks:
        return [], "fast_rank_empty", {}
    cap = max_probes or int(os.environ.get("PUBG_FAST_RANK_MAX", "16"))
    probe = list(peaks)[:cap]
    meta: dict[float, dict[str, Any]] = {}
    scored: list[tuple[float, float, float]] = []
    min_payoff = float(os.environ.get("PUBG_FAST_RANK_MIN_PAYOFF", "0.12"))
    for i, peak in enumerate(probe):
        row = score_peak_fast(video_path, peak, part_sec=part_sec, profile=profile)
        meta[float(peak)] = row
        score = float(row["fast_score"])
        if float(row["payoff_fast"]) < min_payoff:
            score *= 0.55
        if row.get("loot_walk"):
            score *= 0.45
        scored.append((score, -float(i), float(peak)))
    scored.sort(key=lambda x: (-x[0], x[1]))
    ranked = [p for _s, _i, p in scored]
    tail = [p for p in peaks if p not in ranked]
    ranked.extend(tail)
    top = scored[0][0] if scored else 0.0
    return ranked, f"fast_rank top={top:.3f} n={len(probe)}", meta


__all__ = ["rank_peaks_fast", "score_peak_fast"]
