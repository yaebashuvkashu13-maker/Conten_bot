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
            os.environ.get("SHOOTER_VOD_SCAN_COOLDOWN_SEC", "1800"),
        )
        return max(60, int(raw))
    return max(60, int(os.environ.get("SHOOTER_VOD_SCAN_COOLDOWN_SEC", "1800")))


def blocked_rescan_cooldown_sec(game: str, entry: dict[str, Any]) -> int:
    """Shorter retry when peaks exist but were gap-blocked — avoids 2h idle."""
    base = scan_cooldown_sec(game)
    if not entry.get("last_scan_blocked"):
        return base
    peaks = entry.get("last_pool_peaks") or []
    reason = str(entry.get("reject_reason") or "").strip()
    if peaks and not reason:
        short = int(os.environ.get("VOD_GAP_BLOCK_COOLDOWN_SEC", "900"))
        return min(base, max(60, short))
    return base


def strict_peak_tries(game: str = "") -> int:
    """Presend peak attempts per run at strict (L0) — walk pool without exhausting VOD."""
    g = (game or "").strip().lower()
    if g == "mlbb":
        return max(1, int(os.environ.get("MLBB_VOD_STRICT_PEAK_TRIES", "4")))
    return max(1, int(os.environ.get("SHOOTER_VOD_STRICT_PEAK_TRIES", "2")))


def max_peak_tries(soften_level: int, *, game: str, soft_max_fn: Callable[[], int]) -> int:
    if soften_level > 0:
        return soft_max_fn()
    return strict_peak_tries(game)


def should_mark_vod_exhausted(entry: dict[str, Any]) -> bool:
    """Mark exhausted only when no peaks left to try — not on presend reject."""
    if entry.get("last_scan_blocked"):
        return True
    peaks = entry.get("last_pool_peaks")
    if peaks is not None and len(peaks) == 0:
        return True
    return False


def pool_peaks_fully_blocked(
    pool_peaks: list[float] | list[dict[str, Any]] | list[Any],
    *,
    used_peaks: list[float],
    gap_sec: float,
    blocked_sids: set[str],
    vod_id: str,
    lead_sec: float = 4.0,
) -> bool:
    """All highlight peaks already sent, labeled, or within gap of sent peaks."""
    if isinstance(pool_peaks, list) and pool_peaks and isinstance(pool_peaks[0], dict):
        floats = [float(r.get("peak_sec", r.get("start", 0))) for r in pool_peaks]
    else:
        floats = [float(p) for p in pool_peaks]
    if not floats:
        return False
    available, _ = filter_blocked_peaks(floats, used_peaks, gap_sec=gap_sec)
    if available:
        return False
    for peak in floats:
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
    sent_ok = int(entry.get("last_scan_sent") or 0) > 0
    # Zombie rows: blocked forever with no scan timestamp — used to burn every
    # max-vods slot before long inbox files. Skip until an explicit unstall.
    if last <= 0:
        if entry.get("last_scan_blocked") and not sent_ok:
            return True
        return False
    if sent_ok:
        return False
    age = time.time() - last
    if age < blocked_rescan_cooldown_sec(game, entry) and entry.get("last_scan_blocked"):
        return True
    # Zero-send without blocked flag (gate reject / anti-hang) — still cool down
    # so we don't re-dense-scan the same VOD every 8s idle tick.
    zero_cd = int(os.environ.get("VOD_ZERO_SEND_COOLDOWN_SEC", "600"))
    if age < max(60, zero_cd) and str(entry.get("reject_reason") or ""):
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


def pool_ttl_sec() -> int:
    return max(60, int(os.environ.get("VOD_POOL_TTL_SEC", str(6 * 3600))))


def pool_cache_valid(entry: dict[str, Any] | None) -> bool:
    if not entry:
        return False
    raw = entry.get("last_pool_peaks")
    if not raw:
        return False
    last = float(entry.get("last_pool_at") or entry.get("last_scan_at") or 0)
    if last <= 0:
        return False
    return (time.time() - last) < pool_ttl_sec()


def normalize_pool_peak_rows(raw: list[Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in raw:
        if isinstance(item, dict):
            rows.append(
                {
                    "peak_sec": round(float(item.get("peak_sec", item.get("start", 0))), 1),
                    "score": round(float(item.get("score", 0)), 4),
                    "blocked_reason": str(item.get("blocked_reason") or ""),
                }
            )
        else:
            rows.append(
                {
                    "peak_sec": round(float(item), 1),
                    "score": 0.0,
                    "blocked_reason": "",
                }
            )
    return rows


def peak_values_from_entry(entry: dict[str, Any] | None) -> list[float]:
    if not entry:
        return []
    return [float(r["peak_sec"]) for r in normalize_pool_peak_rows(entry.get("last_pool_peaks") or [])]


def minimal_pool_from_entry(entry: dict[str, Any]) -> list[dict[str, Any]]:
    pool: list[dict[str, Any]] = []
    for row in normalize_pool_peak_rows(entry.get("last_pool_peaks") or []):
        if row.get("blocked_reason"):
            continue
        peak = float(row["peak_sec"])
        pool.append(
            {
                "start": peak,
                "peak_start": peak,
                "score": float(row.get("score") or 0),
                "highlight_metrics": {"rule_pass": True, "pass_reason": "cached_pool"},
            }
        )
    return pool


def record_vod_scan(
    entry: dict[str, Any],
    *,
    sent: int,
    pool_peaks: list[float],
    blocked: bool,
    pool: list[dict] | None = None,
    analysis_cache_key: str = "",
) -> None:
    entry["last_scan_at"] = time.time()
    entry["last_scan_sent"] = int(sent)
    entry["last_scan_blocked"] = bool(blocked)
    if pool:
        detail: list[dict[str, Any]] = []
        for clip in pool[:24]:
            peak = round(float(clip.get("start", clip.get("peak_start", 0))), 1)
            detail.append(
                {
                    "peak_sec": peak,
                    "score": round(float(clip.get("score") or 0), 4),
                    "blocked_reason": str(clip.get("blocked_reason") or ""),
                }
            )
        entry["last_pool_peaks"] = detail
        entry["last_pool_at"] = time.time()
    else:
        # Empty list must be written — falsy `elif pool_peaks:` skipped [] and
        # left last_pool_peaks unset, so dead VODs never exhausted (PUBG spam loop).
        entry["last_pool_peaks"] = [
            {"peak_sec": round(float(p), 1), "score": 0.0, "blocked_reason": ""}
            for p in list(pool_peaks or [])[:24]
        ]
        entry["last_pool_at"] = time.time()
    if analysis_cache_key:
        entry["last_analysis_cache_key"] = analysis_cache_key


def peaks_from_pool(pool: list[dict]) -> list[float]:
    return [float(c.get("start", c.get("peak_start", 0))) for c in pool]


def used_peaks_for_vod(
    game: str,
    vod_id: str,
    sent_set: set[str],
    index_segments: list[dict],
) -> list[float]:
    return used_peak_times_shooter(vod_id, sent_set, index_segments)
