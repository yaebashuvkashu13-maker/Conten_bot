"""Time-interval helpers for MLBB VOD highlight dedupe (no heavy deps)."""

from __future__ import annotations

import os


def segment_duration(row: dict) -> float:
    clip = row.get("clip") or {}
    for key in ("input_duration", "output_duration", "fight_dur", "duration"):
        raw = row.get(key)
        if raw is None and key in clip:
            raw = clip.get(key)
        try:
            val = float(raw)
        except (TypeError, ValueError):
            continue
        if val > 0.5:
            return val
    return float(os.environ.get("MLBB_VOD_SEGMENT_SEC", "15"))


def segment_end(row: dict) -> float:
    return float(row["start"]) + segment_duration(row)


def segment_interval(row: dict) -> tuple[float, float]:
    start = float(row["start"])
    return start, segment_end(row)


def interval_gap_sec() -> float:
    return float(os.environ.get("MLBB_VOD_INTERVAL_GAP_SEC", "3"))


def intervals_overlap(
    start_a: float,
    end_a: float,
    start_b: float,
    end_b: float,
    *,
    gap: float = 0.0,
) -> bool:
    """True when [start,end] windows share time (optional gap between them)."""
    if start_b >= end_a + gap:
        return False
    if start_a >= end_b + gap:
        return False
    return True


def conflicts_any_interval(
    start: float,
    end: float,
    intervals: list[tuple[float, float]],
    *,
    gap: float,
) -> bool:
    return any(intervals_overlap(start, end, a0, a1, gap=gap) for a0, a1 in intervals)
