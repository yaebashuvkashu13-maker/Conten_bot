#!/usr/bin/env python3
"""Pick montage parts from nearby fights — sequential timecodes, not spread across VOD."""

from __future__ import annotations

import os
from typing import Any


def sequential_montage_enabled() -> bool:
    return os.environ.get("SHOOTER_VOD_MONTAGE_SEQUENTIAL", "1") == "1"


def montage_part_gap_sec() -> float:
    """Minimum spacing between montage parts (dedupe overlapping fights)."""
    return float(os.environ.get("SHOOTER_VOD_MONTAGE_PART_GAP_SEC", "20"))


def montage_cluster_span_sec() -> float:
    """Max timeline span from first to last peak inside one montage."""
    return float(os.environ.get("SHOOTER_VOD_MONTAGE_CLUSTER_SPAN_SEC", "240"))


def pool_peak_gap_sec() -> float:
    """Min gap when building the ranked peak pool in sequential mode."""
    return float(os.environ.get("SHOOTER_VOD_MONTAGE_POOL_PART_GAP_SEC", "22"))


def _row_peak(row: dict[str, Any]) -> float:
    return float(row.get("peak_start", row.get("start", 0)) or 0)


def _row_score(row: dict[str, Any]) -> float:
    return float(row.get("score", 0) or 0)


def pick_spread_montage_rows(
    rows: list[dict],
    *,
    min_clips: int,
    max_clips: int,
    gap_sec: float,
) -> list[dict]:
    """Greedy highest-score peaks spaced by montage gap (legacy spread)."""
    pool_cap = max(max_clips * 3, min_clips + 3)
    picked: list[dict] = []
    for row in sorted(rows, key=_row_score, reverse=True):
        peak = _row_peak(row)
        if any(abs(peak - _row_peak(p)) < gap_sec for p in picked):
            continue
        picked.append(row)
        if len(picked) >= pool_cap:
            break
    return picked


def pick_sequential_montage_rows(
    rows: list[dict],
    *,
    min_clips: int,
    max_clips: int,
    part_gap_sec: float | None = None,
    cluster_span_sec: float | None = None,
) -> list[dict]:
    """Pick the best dense fight streak — parts stay close in time and play in order."""
    if not rows:
        return []
    part_gap = float(part_gap_sec if part_gap_sec is not None else montage_part_gap_sec())
    cluster_span = float(
        cluster_span_sec if cluster_span_sec is not None else montage_cluster_span_sec()
    )
    pool_cap = max(max_clips * 3, min_clips + 3)

    indexed = sorted(
        ((_row_peak(row), _row_score(row), row) for row in rows),
        key=lambda item: item[0],
    )
    if len(indexed) < min_clips:
        return [row for _, _, row in indexed[:pool_cap]]

    def _select_from_run(
        run: list[tuple[float, float, dict]],
    ) -> list[tuple[float, float, dict]]:
        if len(run) <= max_clips:
            return sorted(run, key=lambda item: item[0])
        best_combo: list[tuple[float, float, dict]] = []
        best_combo_score = -1.0
        n = len(run)
        for i in range(n):
            chosen: list[tuple[float, float, dict]] = []
            for j in range(i, n):
                peak, score, row = run[j]
                if chosen and peak - chosen[0][0] > cluster_span:
                    break
                if any(abs(peak - p) < part_gap for p, _, _ in chosen):
                    continue
                chosen.append((peak, score, row))
                if len(chosen) == max_clips:
                    break
            if len(chosen) < min_clips:
                continue
            combo_score = sum(score for _, score, _ in chosen) + 0.04 * len(chosen)
            if combo_score > best_combo_score:
                best_combo_score = combo_score
                best_combo = sorted(chosen, key=lambda item: item[0])
        if best_combo:
            return best_combo
        by_score = sorted(run, key=lambda item: -item[1])[:max_clips]
        return sorted(by_score, key=lambda item: item[0])

    best_run: list[tuple[float, float, dict]] = []
    best_run_score = -1.0
    n = len(indexed)
    for i in range(n):
        streak: list[tuple[float, float, dict]] = []
        for j in range(i, n):
            peak, score, row = indexed[j]
            if streak and peak - streak[0][0] > cluster_span:
                break
            if any(abs(peak - p) < part_gap for p, _, _ in streak):
                continue
            streak.append((peak, score, row))
        if len(streak) < min_clips:
            continue
        selected = _select_from_run(streak)
        if len(selected) < min_clips:
            continue
        run_score = sum(s for _, s, _ in selected) + 0.05 * len(selected)
        if run_score > best_run_score:
            best_run_score = run_score
            best_run = selected

    if not best_run:
        return pick_spread_montage_rows(
            rows,
            min_clips=min_clips,
            max_clips=max_clips,
            gap_sec=max(part_gap, montage_cluster_span_sec() * 0.35),
        )

    picked = [row for _, _, row in best_run]
    cluster_lo = best_run[0][0] - part_gap
    cluster_hi = best_run[-1][0] + part_gap
    extras: list[dict] = []
    for peak, score, row in indexed:
        if peak < cluster_lo or peak > cluster_hi:
            continue
        if any(str(row.get("segment_id") or "") == str(p.get("segment_id") or "") for p in picked):
            continue
        if any(abs(peak - _row_peak(p)) < part_gap for p in picked + extras):
            continue
        extras.append(row)
    extras.sort(key=_row_score, reverse=True)
    for row in extras:
        if len(picked) >= pool_cap:
            break
        picked.append(row)
    return picked[:pool_cap]


def pick_montage_rows(
    rows: list[dict],
    *,
    min_clips: int,
    max_clips: int,
    gap_sec: float,
) -> list[dict]:
    if sequential_montage_enabled():
        return pick_sequential_montage_rows(
            rows,
            min_clips=min_clips,
            max_clips=max_clips,
            part_gap_sec=min(gap_sec * 0.4, montage_part_gap_sec()) if gap_sec > 0 else None,
        )
    return pick_spread_montage_rows(
        rows,
        min_clips=min_clips,
        max_clips=max_clips,
        gap_sec=gap_sec,
    )


def sequential_pool_peaks(
    scored_centers: list[tuple[float, float]],
    *,
    pool_cap: int,
    part_gap_sec: float | None = None,
) -> list[float]:
    """Chronological peak pool — many fights per hot zone instead of one per VOD hour."""
    if not scored_centers:
        return []
    gap = float(part_gap_sec if part_gap_sec is not None else pool_peak_gap_sec())
    by_time = sorted(scored_centers, key=lambda item: item[1])
    picked: list[tuple[float, float]] = []
    for score, center in by_time:
        if any(abs(center - c) < gap for _, c in picked):
            continue
        picked.append((score, center))
        if len(picked) >= pool_cap:
            break
    picked.sort(key=lambda item: item[1])
    return [center for _, center in picked]


__all__ = [
    "montage_cluster_span_sec",
    "montage_part_gap_sec",
    "pick_montage_rows",
    "pick_sequential_montage_rows",
    "pick_spread_montage_rows",
    "pool_peak_gap_sec",
    "sequential_montage_enabled",
    "sequential_pool_peaks",
]
