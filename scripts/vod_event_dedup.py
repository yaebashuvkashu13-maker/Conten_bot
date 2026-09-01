#!/usr/bin/env python3
"""Event-level deduplication for montage peak pools."""

from __future__ import annotations

import os
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


def filter_montage_peaks(
    peaks: Iterable[float],
    *,
    used: Iterable[float] | None = None,
    gap_sec: float = 55.0,
    scores: dict[float, float] | None = None,
) -> list[float]:
    """Drop peaks too close to already-used montage parts."""
    used_list = [float(u) for u in (used or [])]
    merged = merge_nearby_peaks(peaks, scores=scores)
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


__all__ = ["filter_montage_peaks", "merge_nearby_peaks", "peaks_too_similar"]
