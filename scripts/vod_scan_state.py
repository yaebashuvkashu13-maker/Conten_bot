#!/usr/bin/env python3
"""PUBG/shooter VOD scan state — avoid re-running expensive highlight on dead VODs."""

from __future__ import annotations

import os
import time
from typing import Any

from vod_peak_gap import filter_blocked_peaks, peak_too_close, used_peak_times_shooter


def scan_cooldown_sec() -> int:
    return max(60, int(os.environ.get("SHOOTER_VOD_SCAN_COOLDOWN_SEC", "7200")))


def should_mark_vod_exhausted(entry: dict[str, Any]) -> bool:
    """Mark exhausted only when no peaks left to try — not on presend reject."""
    if entry.get("last_scan_blocked"):
        return True
    peaks = entry.get("last_pool_peaks")
    if peaks is not None and len(peaks) == 0:
        return True
    return False


def pool_peaks_fully_blocked(
    pool_peaks: list[float],
    *,
    used_peaks: list[float],
    gap_sec: float,
    blocked_sids: set[str],
    vod_id: str,
    lead_sec: float = 4.0,
) -> bool:
    """All highlight peaks already sent, labeled, or within gap of sent peaks."""
    if not pool_peaks:
        return False
    available, _ = filter_blocked_peaks(pool_peaks, used_peaks, gap_sec=gap_sec)
    if available:
        return False
    # Also check segment ids for sent/labeled (peak 124 → start 120).
    for peak in pool_peaks:
        start = max(0.0, peak - lead_sec)
        sid = f"{vod_id}_{int(start)}"
        if sid not in blocked_sids:
            if not peak_too_close(peak, used_peaks, gap_sec):
                return False
    return True


def should_skip_vod_rescan(entry: dict[str, Any] | None) -> bool:
    if not entry:
        return False
    if entry.get("exhausted"):
        return True
    last = float(entry.get("last_scan_at") or 0)
    if last <= 0:
        return False
    if int(entry.get("last_scan_sent") or 0) > 0:
        return False
    age = time.time() - last
    if age < scan_cooldown_sec() and entry.get("last_scan_blocked"):
        return True
    return False


def record_vod_scan(
    entry: dict[str, Any],
    *,
    sent: int,
    pool_peaks: list[float],
    blocked: bool,
) -> None:
    entry["last_scan_at"] = time.time()
    entry["last_scan_sent"] = int(sent)
    entry["last_scan_blocked"] = bool(blocked)
    if pool_peaks:
        entry["last_pool_peaks"] = [round(p, 1) for p in pool_peaks[:12]]


def peaks_from_pool(pool: list[dict]) -> list[float]:
    return [float(c.get("start", 0)) for c in pool]


def used_peaks_for_vod(
    game: str,
    vod_id: str,
    sent_set: set[str],
    index_segments: list[dict],
) -> list[float]:
    return used_peak_times_shooter(vod_id, sent_set, index_segments)
