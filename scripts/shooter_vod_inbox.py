#!/usr/bin/env python3
"""Shooter VOD inbox backlog — block discovery while local files need scanning."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Callable


def discovery_max_inbox() -> int:
    return max(0, int(os.environ.get("SHOOTER_VOD_DISCOVERY_MAX_INBOX", "10")))


def force_full_scan_backlog() -> int:
    return max(0, int(os.environ.get("SHOOTER_VOD_FORCE_FULL_SCAN_BACKLOG", "8")))


def retryable_reject_reason(reason: str) -> bool:
    r = str(reason or "").strip()
    if not r:
        return True
    if r.startswith(
        (
            "score_timeout",
            "combat_gate_fail",
            "presend_exhausted",
            "all_peaks_blocked",
            "fast_panns",
            "fast_probe",
        )
    ):
        return True
    if r.startswith("no_combat_peaks"):
        return True
    return False


def fast_probe_top(fast_reason: str) -> float:
    m = re.search(r"top=([0-9.]+)", str(fast_reason or ""))
    return float(m.group(1)) if m else 0.0


def registry_entry(registry: list[dict], mp4: Path, *, vod_id_fn: Callable[[Path], str]) -> dict | None:
    entry = next((r for r in registry if r.get("path") == str(mp4)), None)
    if entry is not None:
        return entry
    vid = vod_id_fn(mp4)
    return next((r for r in registry if r.get("id") == vid), None)


def long_inbox_vods(
    inbox: Path,
    *,
    min_sec: float,
    duration_fn: Callable[[Path], float],
) -> list[Path]:
    out: list[Path] = []
    for mp4 in inbox.glob("yt_*.mp4"):
        if duration_fn(mp4) < min_sec:
            continue
        out.append(mp4)
    out.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return out


def pending_inbox_work(
    inbox: Path,
    registry: list[dict],
    *,
    min_sec: float,
    duration_fn: Callable[[Path], float],
    vod_id_fn: Callable[[Path], str],
) -> int:
    n = 0
    for mp4 in long_inbox_vods(inbox, min_sec=min_sec, duration_fn=duration_fn):
        entry = registry_entry(registry, mp4, vod_id_fn=vod_id_fn)
        if entry is None or not entry.get("exhausted"):
            n += 1
            continue
        if retryable_reject_reason(str(entry.get("reject_reason") or "")):
            n += 1
    return n


def discovery_blocked(
    inbox: Path,
    registry: list[dict],
    *,
    min_sec: float,
    duration_fn: Callable[[Path], float],
    vod_id_fn: Callable[[Path], str],
) -> bool:
    cap = discovery_max_inbox()
    if cap <= 0:
        return False
    long_count = len(long_inbox_vods(inbox, min_sec=min_sec, duration_fn=duration_fn))
    pending = pending_inbox_work(
        inbox, registry, min_sec=min_sec, duration_fn=duration_fn, vod_id_fn=vod_id_fn
    )
    if long_count >= cap:
        return True
    if pending > 0 and long_count >= max(3, cap // 2):
        return True
    return False


def reopen_inbox_backlog(
    registry: list[dict],
    inbox: Path,
    *,
    min_sec: float,
    duration_fn: Callable[[Path], float],
    vod_id_fn: Callable[[Path], str],
    log_fn: Callable[[str, str], None] | None = None,
) -> int:
    limit = max(0, int(os.environ.get("SHOOTER_VOD_REOPEN_BACKLOG", "12")))
    if limit <= 0:
        return 0
    reopened = 0
    for mp4 in long_inbox_vods(inbox, min_sec=min_sec, duration_fn=duration_fn):
        if reopened >= limit:
            break
        entry = registry_entry(registry, mp4, vod_id_fn=vod_id_fn)
        if not entry or not entry.get("exhausted"):
            continue
        if not retryable_reject_reason(str(entry.get("reject_reason") or "")):
            continue
        entry["exhausted"] = False
        entry["last_scan_at"] = 0
        entry.pop("reject_reason", None)
        entry.pop("presend_reject_streak", None)
        reopened += 1
        if log_fn:
            log_fn("reopen backlog vod=%s for full scan", mp4.name)
    return reopened
