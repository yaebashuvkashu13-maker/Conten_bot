#!/usr/bin/env python3
"""Owner style reference fights — rank and cluster like the target moment (e.g. Tovruh part 2)."""

from __future__ import annotations

import math
import os
from pathlib import Path
from typing import Any

# Explicit style anchors when labels not yet synced on VPS.
PUBG_STYLE_REF_BY_VOD: dict[str, list[float]] = {
    "Tovruh33adY": [5266.0],
}

_STYLE_KEYS = (
    "gunfire_density",
    "panns_gun_max",
    "notification_score",
    "payoff_fast",
    "fight_fast",
)
_STYLE_WEIGHTS = (0.14, 0.14, 0.32, 0.28, 0.12)


def _video_id(vod: Path) -> str:
    stem = vod.stem
    if stem.startswith("yt_") and len(stem) > 3:
        return stem[3:]
    return stem


def _label_is_style_ref(row: dict[str, Any]) -> bool:
    if str(row.get("role") or "").lower() in {"style_ref", "style", "reference"}:
        return str(row.get("label") or "") == "good"
    note = str(row.get("note") or "").lower()
    return "style ref" in note or "target fight" in note or "эталон" in note


def style_reference_peaks(vod: Path) -> list[float]:
    """Owner-marked fights to imitate (not every good label — only style_ref)."""
    vid = _video_id(vod)
    peaks: list[float] = list(PUBG_STYLE_REF_BY_VOD.get(vid, []))
    try:
        from pubg_owner_calibration import labels_for_video

        for row in labels_for_video(vod):
            if not _label_is_style_ref(row):
                continue
            try:
                peaks.append(float(row["time_sec"]))
            except (KeyError, TypeError, ValueError):
                continue
    except ImportError:
        pass
    peaks.sort()
    deduped: list[float] = []
    for peak in peaks:
        if any(abs(peak - p) <= 4.0 for p in deduped):
            continue
        deduped.append(peak)
    return deduped


def style_avoid_peaks(vod: Path) -> list[float]:
    """Fights to deprioritize (wrong intro/payoff shape)."""
    vid = _video_id(vod)
    avoid: list[float] = []
    if vid == "Tovruh33adY":
        avoid.append(1533.0)
    try:
        from pubg_owner_calibration import labels_for_video

        for row in labels_for_video(vod):
            if str(row.get("label") or "") != "bad":
                continue
            note = str(row.get("note") or "").lower()
            if "intro" in note or "cut" in note or "fight" in note or row.get("role") == "anti_style":
                try:
                    avoid.append(float(row["time_sec"]))
                except (KeyError, TypeError, ValueError):
                    continue
    except ImportError:
        pass
    avoid.sort()
    deduped: list[float] = []
    for peak in avoid:
        if any(abs(peak - p) <= 4.0 for p in deduped):
            continue
        deduped.append(peak)
    return deduped


def _feature_vector(row: dict[str, Any]) -> list[float]:
    return [float(row.get(key, 0.0) or 0.0) for key in _STYLE_KEYS]


def _weighted_similarity(reference: dict[str, Any], candidate: dict[str, Any]) -> float:
    ref = _feature_vector(reference)
    cand = _feature_vector(candidate)
    num = 0.0
    den = 0.0
    for weight, a, b in zip(_STYLE_WEIGHTS, ref, cand, strict=True):
        num += weight * a * b
        den += weight * max(a, b, 1e-6)
    if den <= 0:
        return 0.0
    return max(0.0, min(1.0, num / den))


def build_style_profile(
    vod: Path,
    peaks: list[float],
    *,
    part_sec: float = 14.0,
) -> dict[str, Any] | None:
    if not peaks:
        return None
    from pubg_fast_peak_rank import score_peak_fast

    rows = [score_peak_fast(vod, peak, part_sec=part_sec, profile="pubg") for peak in peaks]
    if not rows:
        return None
    profile: dict[str, Any] = {"peaks": list(peaks)}
    for key in _STYLE_KEYS:
        profile[key] = sum(float(row.get(key, 0.0) or 0.0) for row in rows) / len(rows)
    profile["notification_hit"] = sum(1 for row in rows if row.get("notification_hit")) / len(rows)
    return profile


def style_similarity(profile: dict[str, Any] | None, peak_row: dict[str, Any]) -> float:
    if not profile:
        return 0.5
    sim = _weighted_similarity(profile, peak_row)
    if profile.get("notification_hit", 0) >= 0.5 and peak_row.get("notification_hit"):
        sim = min(1.0, sim + 0.12)
    if float(peak_row.get("payoff_fast", 0.0) or 0.0) < 0.15:
        sim *= 0.72
    if peak_row.get("loot_walk"):
        sim *= 0.55
    return max(0.0, min(1.0, sim))


def rank_peaks_by_style(
    vod: Path,
    peaks: list[float],
    *,
    part_sec: float = 14.0,
    meta: dict[float, dict[str, Any]] | None = None,
) -> tuple[list[float], str, dict[float, float]]:
    """Reorder peaks toward owner style reference; penalize anti-style windows."""
    if not peaks:
        return [], "style_rank_empty", {}
    refs = style_reference_peaks(vod)
    avoid = style_avoid_peaks(vod)
    if not refs and not avoid:
        return list(peaks), "style_rank_skip", {}

    from pubg_fast_peak_rank import score_peak_fast

    profile = build_style_profile(vod, refs, part_sec=part_sec) if refs else None
    avoid_profile = build_style_profile(vod, avoid, part_sec=part_sec) if avoid else None
    blend = float(os.environ.get("PUBG_STYLE_RANK_BLEND", "0.58"))
    sims: dict[float, float] = {}
    scored: list[tuple[float, float, float]] = []
    for index, peak in enumerate(peaks):
        row = (meta or {}).get(float(peak))
        if row is None:
            row = score_peak_fast(vod, peak, part_sec=part_sec, profile="pubg")
        sim = style_similarity(profile, row)
        if avoid_profile is not None:
            anti = style_similarity(avoid_profile, row)
            sim = max(0.0, sim - anti * float(os.environ.get("PUBG_STYLE_ANTI_BLEND", "0.45")))
        sims[float(peak)] = round(sim, 4)
        base = float(row.get("fast_score", row.get("score", 0.5)) or 0.5)
        composite = base * (1.0 - blend) + sim * blend
        scored.append((composite, -float(index), float(peak)))
    scored.sort(key=lambda item: (-item[0], item[1]))
    ranked = [peak for _, _, peak in scored]
    tail = [p for p in peaks if p not in ranked]
    ranked.extend(tail)
    top_sim = sims.get(float(scored[0][2]), 0.0) if scored else 0.0
    reason = f"style_rank refs={len(refs)} avoid={len(avoid)} top_sim={top_sim:.3f}"
    return ranked, reason, sims


def nearest_style_reference(peak_sec: float, refs: list[float], *, radius: float = 180.0) -> float | None:
    if not refs:
        return None
    best: float | None = None
    best_dist = radius + 1.0
    for ref in refs:
        dist = abs(float(peak_sec) - float(ref))
        if dist <= radius and dist < best_dist:
            best = float(ref)
            best_dist = dist
    return best


def cluster_anchor_bonus(
    peaks: list[float],
    refs: list[float],
    *,
    cluster_span_sec: float,
) -> float:
    if not refs or not peaks:
        return 0.0
    span = max(peaks) - min(peaks)
    bonus = 0.0
    for ref in refs:
        inside = [p for p in peaks if abs(p - ref) <= cluster_span_sec * 0.55]
        if len(inside) >= 1:
            bonus += 1.5 + 0.25 * len(inside)
            nearest = min(abs(p - ref) for p in peaks)
            bonus += max(0.0, 1.2 - nearest / max(cluster_span_sec, 1.0))
    if span > cluster_span_sec * 1.15:
        bonus *= 0.55
    return bonus


__all__ = [
    "build_style_profile",
    "cluster_anchor_bonus",
    "rank_peaks_by_style",
    "style_avoid_peaks",
    "style_reference_peaks",
    "style_similarity",
]
