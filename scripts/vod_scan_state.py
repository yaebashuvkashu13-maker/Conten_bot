#!/usr/bin/env python3
"""VOD scan state — avoid re-running expensive highlight on dead VODs (all games)."""

from __future__ import annotations

import os
import time
from typing import Any, Callable

from vod_peak_gap import filter_blocked_peaks, peak_too_close, used_peak_times_shooter


def scan_cooldown_sec(game: str = "") -> int:
    g = (game or "").strip().lower()
    if g == "mlbb":
        raw = os.environ.get(
            "MLBB_VOD_SCAN_COOLDOWN_SEC",
            os.environ.get("SHOOTER_VOD_SCAN_COOLDOWN_SEC", "7200"),
        )
        return max(60, int(raw))
    return max(60, int(os.environ.get("SHOOTER_VOD_SCAN_COOLDOWN_SEC", "7200")))


def strict_peak_tries(game: str = "") -> int:
    """Presend peak attempts per run at strict (L0) — walk pool without exhausting VOD."""
    g = (game or "").strip().lower()
    if g == "mlbb":
        return max(1, int(os.environ.get("MLBB_VOD_STRICT_PEAK_TRIES", "2")))
    return max(1, int(os.environ.get("SHOOTER_VOD_STRICT_PEAK_TRIES", "2")))


def max_peak_tries(soften_level: int, *, game: str, soft_max_fn: Callable[[], int]) -> int:
    if soften_level > 0:
        return soft_max_fn()
    return strict_peak_tries(game)


def should_mark_vod_exhausted(entry: dict[str, Any]) -> bool:
    """Mark exhausted when no peaks left, scan blocked, or repeated presend rejects."""
    if entry.get("last_scan_blocked"):
        return True
    peaks = entry.get("last_pool_peaks")
    if peaks is not None and len(peaks) == 0:
        return True
    presend_limit = max(1, int(os.environ.get("MLBB_VOD_PRESEND_EXHAUST_AFTER", "3")))
    if int(entry.get("presend_reject_streak") or 0) >= presend_limit:
        return True
    if str(entry.get("reject_reason") or "") in {"scan_timeout", "presend_exhausted"}:
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


def should_skip_vod_rescan(entry: dict[str, Any] | None, *, game: str = "") -> bool:
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
    if age < scan_cooldown_sec(game) and entry.get("last_scan_blocked"):
        return True
    if int(entry.get("presend_reject_streak") or 0) > 0 and age < scan_cooldown_sec(game):
        return True
    if str(entry.get("reject_reason") or "") == "scan_timeout" and age < scan_cooldown_sec(game):
        return True
    return False


def scan_zero_detail(entry: dict[str, Any] | None) -> str:
    """Human-readable reason for zero-send scan (Telegram diagnostics)."""
    if not entry:
        return ""
    if entry.get("last_scan_blocked"):
        return "все пики заняты или отправлены"
    peaks = entry.get("last_pool_peaks")
    if peaks is not None and len(peaks) == 0:
        return "нет боёв в VOD (highlight/panns pool=0)"
    reason = str(entry.get("reject_reason") or "").strip()
    if reason:
        return reason[:140]
    if peaks:
        return f"presend отклонил пики (pool={len(peaks)})"
    return ""


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
