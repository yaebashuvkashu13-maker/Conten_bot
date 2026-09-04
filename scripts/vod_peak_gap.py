#!/usr/bin/env python3
"""Shared peak-gap logic — bad labels must not block nearby highlight peaks."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Callable


def peak_too_close(peak: float, used_peaks: list[float], gap_sec: float) -> bool:
    return any(abs(peak - p) <= gap_sec for p in used_peaks)


def coerce_peak_sec(peak: object) -> float | None:
    """Normalize pool peak entries (float or ``{"peak_sec": ...}``) to seconds."""
    if isinstance(peak, dict):
        raw = peak.get("peak_sec", peak.get("peak", peak.get("t")))
    else:
        raw = peak
    try:
        return float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def pool_peak_seconds(peaks: list | tuple | None) -> list[float]:
    out: list[float] = []
    for peak in peaks or []:
        val = coerce_peak_sec(peak)
        if val is not None:
            out.append(val)
    return out


def segment_gap_sec(
    game: str,
    *,
    soften_level: int = 0,
    default: float | None = None,
) -> float:
    """Gap between peaks on the same VOD. Shrinks when adaptive soften is active."""
    g = game.strip().lower()
    if g == "mlbb":
        base = default if default is not None else float(
            os.environ.get("MLBB_VOD_SEGMENT_GAP_SEC", os.environ.get("HIGHLIGHT_MIN_GAP_SEC", "90"))
        )
        env_key = "MLBB_VOD_SEGMENT_GAP_SEC"
        soft_key = "MLBB_VOD_SOFT_SEGMENT_GAP_SEC"
        soft_default = 28.0
    else:
        base = default if default is not None else 18.0
        env_key = "SHOOTER_VOD_SEGMENT_GAP_SEC"
        soft_key = "SHOOTER_VOD_SOFT_SEGMENT_GAP_SEC"
        soft_default = 7.0

    gap = float(os.environ.get(env_key, str(base)))
    if soften_level >= 2:
        gap = min(gap, float(os.environ.get(soft_key, str(soft_default))))
    elif soften_level >= 1 and g == "mlbb":
        gap = min(gap, float(os.environ.get(soft_key, str(soft_default * 1.6))))
    return gap


def used_peak_times_shooter(
    vod_id: str,
    sent_set: set[str],
    index_segments: list[dict],
) -> list[float]:
    """Peak times from sent shooter segments (PUBG / Standoff / WoT).

    Dedupes aggressively: each montage part sid historically re-appended the full
    ``montage_parts`` list, inflating used=21 from 3 real fights and starving ×3.
    """
    index = {str(s.get("segment_id")): s for s in index_segments}
    peaks: set[float] = set()
    expanded_montages: set[str] = set()

    def _add_part_sid(part_sid: str) -> None:
        try:
            part_sid = str(part_sid)
            if part_sid.startswith(f"{vod_id}_"):
                peaks.add(float(part_sid[len(vod_id) + 1 :].rsplit("_", 1)[-1]))
        except ValueError:
            pass

    for sid in sent_set:
        if not sid.startswith(f"{vod_id}_"):
            continue
        row = index.get(sid)
        if row and row.get("peak_start") is not None:
            peaks.add(float(row["peak_start"]))
            montage_key = str(row.get("montage_id") or sid)
            if montage_key not in expanded_montages:
                expanded_montages.add(montage_key)
                for extra in row.get("montage_peaks") or []:
                    try:
                        peaks.add(float(extra))
                    except (TypeError, ValueError):
                        pass
                # Legacy: part sids list. Broken writers stored an int part-count —
                # never iterate that (TypeError crashed every post-send Standoff scan).
                parts = row.get("montage_parts")
                if isinstance(parts, (list, tuple)):
                    for part_sid in parts:
                        _add_part_sid(str(part_sid))
            continue
        tail = sid[len(vod_id) + 1 :]
        # Ignore composite montage ids like m90_152_330 — parts are tracked separately.
        if tail.startswith("m") and "_" in tail:
            # Still recover peaks embedded in the id when index row is missing.
            try:
                for x in tail[1:].split("_"):
                    if x.replace(".", "", 1).isdigit():
                        peaks.add(float(x))
            except ValueError:
                pass
            continue
        try:
            peaks.add(float(tail.rsplit("_", 1)[-1]))
        except ValueError:
            continue
    return sorted(peaks)


def load_index_segments(index_path: Path) -> list[dict]:
    if not index_path.exists():
        return []
    try:
        return list(json.loads(index_path.read_text(encoding="utf-8")).get("segments", []))
    except (json.JSONDecodeError, OSError):
        return []


def reserved_sent_only() -> bool:
    return os.environ.get("MLBB_VOD_RESERVED_SENT_ONLY", "0") == "1"


def filter_blocked_peaks(
    pool_peaks: list[float],
    used_peaks: list[float],
    *,
    gap_sec: float,
    skip_peaks: set[float] | None = None,
    skip_tol: float = 4.0,
) -> tuple[list[float], list[float]]:
    """Return (available, blocked) peak lists for diagnostics."""
    skip_peaks = skip_peaks or set()
    available: list[float] = []
    blocked: list[float] = []
    for peak in pool_peaks:
        if any(abs(peak - s) <= skip_tol for s in skip_peaks):
            blocked.append(peak)
            continue
        if peak_too_close(peak, used_peaks, gap_sec):
            blocked.append(peak)
            continue
        available.append(peak)
    return available, blocked
