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


def banner_hits_in_entry(entry: dict[str, Any] | None) -> int:
    """How many kill-banner peaks were seen on the last scan."""
    if not entry:
        return 0
    explicit = entry.get("last_banner_hits")
    if explicit is not None:
        return max(0, int(explicit))
    n = 0
    for row in entry.get("last_pool_peaks") or []:
        if not isinstance(row, dict):
            continue
        if int(row.get("kill_banner_tier") or 0) > 0 or row.get("kill_banner"):
            n += 1
    return n


def should_retry_banner_gap(entry: dict[str, Any] | None) -> bool:
    """
    Banner discover found fights, but segment gap / prior used peaks blocked them.
    Keep the VOD for a softer-gap retry instead of deleting it.
    """
    if not entry or os.environ.get("MLBB_VOD_BANNER_GAP_RETRY", "1") != "1":
        return False
    # Already shipped this fight — discovering the same kill again is not a gap issue.
    if str(entry.get("reject_reason") or "") in {"already_sent", "all_peaks_blocked"}:
        return False
    if not entry.get("last_scan_blocked"):
        return False
    if banner_hits_in_entry(entry) <= 0:
        return False
    max_retries = max(0, int(os.environ.get("MLBB_VOD_BANNER_GAP_RETRIES", "2")))
    return int(entry.get("banner_gap_retries") or 0) < max_retries


def should_mark_vod_exhausted(entry: dict[str, Any]) -> bool:
    """Mark exhausted only when no peaks left to try — not on presend reject."""
    if entry.get("last_scan_blocked"):
        if should_retry_banner_gap(entry):
            return False
        return True
    peaks = entry.get("last_pool_peaks")
    if peaks is not None and len(peaks) == 0:
        return True
    max_attempts = max(
        3,
        int(
            os.environ.get(
                "SHOOTER_VOD_MAX_ZERO_ATTEMPTS",
                os.environ.get("MLBB_VOD_MAX_ZERO_ATTEMPTS", "5"),
            )
        ),
    )
    if int(entry.get("zero_send_attempts") or 0) >= max_attempts:
        # Still allow banner-gap soft retries before permanent exhaust.
        if should_retry_banner_gap(entry):
            return False
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
    # Soft banner-gap retries must not wait out the full cooldown.
    if should_retry_banner_gap(entry) or int(entry.get("banner_gap_retries") or 0) > 0:
        if entry.get("reject_reason") in {"banner_gap_retry", "banner_hits_no_send"}:
            return False
    last = float(entry.get("last_scan_at") or 0)
    if last <= 0:
        return False
    if int(entry.get("last_scan_sent") or 0) > 0:
        return False
    age = time.time() - last
    if age < scan_cooldown_sec(game) and entry.get("last_scan_blocked"):
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
    # Short TTL — stale 6h pools reused wrong peaks (UGu 310 vs LEGENDARY @312).
    return max(60, int(os.environ.get("VOD_POOL_TTL_SEC", "900")))


def invalidate_pool_cache(entry: dict[str, Any] | None, *, reason: str = "") -> None:
    """Drop cached peak pool so the next pass rediscovers banners."""
    if not entry:
        return
    entry["last_pool_peaks"] = []
    entry["last_pool_at"] = 0
    entry["last_banner_hits"] = 0
    if reason:
        entry["pool_invalidated"] = str(reason)[:120]


def pool_cache_valid(entry: dict[str, Any] | None) -> bool:
    if os.environ.get("MLBB_VOD_REUSE_PEAK_POOL", "1") != "1":
        return False
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
            row: dict[str, Any] = {
                "peak_sec": round(float(item.get("peak_sec", item.get("start", 0))), 1),
                "score": round(float(item.get("score", 0)), 4),
                "blocked_reason": str(item.get("blocked_reason") or ""),
                "kill_banner_tier": int(item.get("kill_banner_tier") or 0),
                "kill_banner": str(item.get("kill_banner") or ""),
            }
            # Must survive cache round-trip — dropping these caused UGu
            # neg_ref:no_banner (motion peak 310, banner flash 312).
            if item.get("banner_sec") is not None:
                try:
                    row["banner_sec"] = round(float(item.get("banner_sec")), 1)
                except (TypeError, ValueError):
                    pass
            if item.get("banner_source"):
                row["banner_source"] = str(item.get("banner_source") or "")
            if item.get("kill_banner_text") or item.get("banner_text"):
                row["kill_banner_text"] = str(
                    item.get("kill_banner_text") or item.get("banner_text") or ""
                )
            rows.append(row)
        else:
            rows.append(
                {
                    "peak_sec": round(float(item), 1),
                    "score": 0.0,
                    "blocked_reason": "",
                    "kill_banner_tier": 0,
                    "kill_banner": "",
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
        banner_sec = float(row["banner_sec"]) if row.get("banner_sec") is not None else peak
        # Prefer banner flash as peak_start when it differs from motion peak.
        peak_start = banner_sec if abs(banner_sec - peak) >= 0.5 else peak
        clip: dict[str, Any] = {
            "start": peak_start,
            "peak_start": peak_start,
            "score": float(row.get("score") or 0),
            "highlight_metrics": {"rule_pass": True, "pass_reason": "cached_pool"},
        }
        tier = int(row.get("kill_banner_tier") or 0)
        label = str(row.get("kill_banner") or "")
        if tier > 0:
            clip["kill_banner_tier"] = tier
            clip["kill_banner"] = label or "double"
            clip["anchor"] = "kill_banner"
            clip["banner_sec"] = banner_sec
            if row.get("banner_source"):
                clip["banner_source"] = str(row["banner_source"])
            if row.get("kill_banner_text"):
                clip["banner_text"] = str(row["kill_banner_text"])
                clip["kill_banner_text"] = str(row["kill_banner_text"])
        pool.append(clip)
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
        banner_hits = 0
        for clip in pool[:24]:
            peak = round(float(clip.get("start", clip.get("peak_start", 0))), 1)
            tier = int(clip.get("kill_banner_tier") or 0)
            label = str(clip.get("kill_banner") or "")
            if tier > 0 or label:
                banner_hits += 1
            row_detail: dict[str, Any] = {
                "peak_sec": peak,
                "score": round(float(clip.get("score") or 0), 4),
                "blocked_reason": str(clip.get("blocked_reason") or ""),
                "kill_banner_tier": tier,
                "kill_banner": label,
            }
            if clip.get("banner_sec") is not None:
                row_detail["banner_sec"] = round(float(clip["banner_sec"]), 1)
            src = clip.get("banner_source") or clip.get("kill_banner_source")
            if src:
                row_detail["banner_source"] = str(src)
            txt = clip.get("kill_banner_text") or clip.get("banner_text")
            if txt:
                row_detail["kill_banner_text"] = str(txt)[:120]
            detail.append(row_detail)
        entry["last_pool_peaks"] = detail
        entry["last_pool_at"] = time.time()
        entry["last_banner_hits"] = banner_hits
    elif pool_peaks:
        entry["last_pool_peaks"] = [
            {"peak_sec": round(p, 1), "score": 0.0, "blocked_reason": ""}
            for p in pool_peaks[:24]
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
