#!/usr/bin/env python3
"""Event-level deduplication for montage peak pools."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable


def merge_nearby_peaks(
    peaks: Iterable[float],
    *,
    merge_gap_sec: float | None = None,
    scores: dict[float, float] | None = None,
) -> list[float]:
    """Merge peaks within merge_gap_sec — keep highest score (or earliest)."""
    gap = float(
        merge_gap_sec
        if merge_gap_sec is not None
        else os.environ.get("VOD_EVENT_MERGE_GAP_SEC", "25")
    )
    ordered = sorted((float(p) for p in peaks), key=lambda p: -(scores or {}).get(p, 0.0))
    kept: list[float] = []
    for peak in ordered:
        if any(abs(peak - old) < gap for old in kept):
            continue
        kept.append(peak)
    return sorted(kept)


def _audio_signature(video_path: Path, peak_sec: float, window: float = 4.0) -> tuple[float, ...]:
    from highlight_scorer import score_panns_audio

    feats = score_panns_audio(video_path, max(0.0, peak_sec - window * 0.5), window)
    return tuple(round(float(feats.get(k, 0.0)), 4) for k in (
        "panns_gunshot",
        "panns_machine_gun",
        "panns_explosion",
        "panns_speech",
        "panns_music",
    ))


def _signature_distance(a: tuple[float, ...], b: tuple[float, ...]) -> float:
    if not a or not b:
        return 1.0
    return sum((x - y) ** 2 for x, y in zip(a, b, strict=False)) ** 0.5


def dedup_by_audio_signature(
    video_path: Path,
    peaks: Iterable[float],
    *,
    max_distance: float | None = None,
) -> list[float]:
    """Drop peaks with near-identical PANNs audio fingerprints (replay / same fight)."""
    if os.environ.get("VOD_EVENT_AUDIO_DEDUP", "1") != "1":
        return list(peaks)
    limit = float(max_distance or os.environ.get("VOD_EVENT_AUDIO_DEDUP_MAX", "0.12"))
    ordered = sorted((float(p) for p in peaks))
    kept: list[float] = []
    sigs: list[tuple[float, ...]] = []
    for peak in ordered:
        sig = _audio_signature(video_path, peak)
        if any(_signature_distance(sig, old) < limit for old in sigs):
            continue
        kept.append(peak)
        sigs.append(sig)
    return kept


def filter_montage_peaks(
    peaks: Iterable[float],
    *,
    used: Iterable[float] | None = None,
    gap_sec: float = 55.0,
    scores: dict[float, float] | None = None,
    video_path: Path | None = None,
) -> list[float]:
    """Drop peaks too close to already-used montage parts."""
    used_list = [float(u) for u in (used or [])]
    merged = merge_nearby_peaks(peaks, scores=scores)
    if video_path is not None and video_path.is_file():
        merged = dedup_by_audio_signature(video_path, merged)
    out: list[float] = []
    for peak in merged:
        if any(abs(peak - u) < gap_sec for u in used_list):
            continue
        if any(abs(peak - o) < gap_sec * 0.85 for o in out):
            continue
        out.append(peak)
    return out


def peaks_too_similar(
    a: float,
    b: float,
    *,
    gap_sec: float = 25.0,
) -> bool:
    return abs(float(a) - float(b)) < float(gap_sec)


__all__ = [
    "dedup_by_audio_signature",
    "filter_montage_peaks",
    "merge_nearby_peaks",
    "peaks_too_similar",
]
