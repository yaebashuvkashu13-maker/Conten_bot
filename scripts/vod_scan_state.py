#!/usr/bin/env python3
"""VOD scan state — avoid re-running expensive highlight on dead VODs (all games)."""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any, Callable

from mlbb_vod_intervals import conflicts_any_interval, interval_gap_sec
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


def min_probe_start_sec() -> float:
    raw = (os.environ.get("SHOOTER_VOD_MIN_PROBE_START") or "").strip()
    if not raw:
        return 0.0
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 0.0


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


def peaks_near_sent_reason(entry: dict[str, Any] | None) -> bool:
    if not entry:
        return False
    return str(entry.get("reject_reason") or "").startswith("peaks_near_sent")


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
    if reason.startswith("peaks_near_sent"):
        sent = entry.get("last_sent_peaks") or []
        return f"пики рядом с уже отправленными (sent={sent})"
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
    reject_reason: str = "",
) -> None:
    entry["last_scan_at"] = time.time()
    entry["last_scan_sent"] = int(sent)
    entry["last_scan_blocked"] = bool(blocked)
    if reject_reason:
        entry["reject_reason"] = reject_reason
    elif sent > 0:
        entry.pop("reject_reason", None)
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
    blocked_ids: set[str],
    index_segments: list[dict],
) -> list[float]:
    return used_peak_times_shooter(vod_id, blocked_ids, index_segments)


def shooter_fight_peak_span_sec(game: str) -> float:
    g = game.strip().lower()
    if g == "pubg":
        return float(
            os.environ.get(
                "SHOOTER_VOD_FIGHT_PEAK_SPAN_SEC",
                os.environ.get("PUBG_FIGHT_MAX_SEC", "45"),
            )
        )
    return float(os.environ.get("SHOOTER_VOD_FIGHT_PEAK_SPAN_SEC", "30"))


def shooter_peak_fight_blocked(
    peak: float,
    used_peaks: list[float],
    *,
    game: str,
    soften_gap: float,
) -> bool:
    """Block peaks inside the same fight window as an already-sent/labeled peak."""
    span = shooter_fight_peak_span_sec(game)
    factor = float(os.environ.get("SHOOTER_VOD_FIGHT_PEAK_SPAN_FACTOR", "0.40"))
    min_gap = max(soften_gap, span * factor)
    return peak_too_close(peak, used_peaks, min_gap)


def parse_exclude_intervals(raw: str = "") -> list[tuple[float, float]]:
    """Parse HIGHLIGHT_EXCLUDE_INTERVALS env: 'lo-hi;lo-hi'."""
    text = (raw or os.environ.get("HIGHLIGHT_EXCLUDE_INTERVALS", "")).strip()
    if not text:
        return []
    out: list[tuple[float, float]] = []
    for part in text.split(";"):
        part = part.strip()
        if "-" not in part:
            continue
        lo_s, hi_s = part.split("-", 1)
        try:
            lo, hi = float(lo_s), float(hi_s)
        except ValueError:
            continue
        if hi > lo:
            out.append((lo, hi))
    return out


def exclude_intervals_env(
    sent_intervals: list[tuple[float, float]],
    *,
    pad_sec: float | None = None,
) -> str:
    if not sent_intervals:
        return ""
    pad = float(
        pad_sec
        if pad_sec is not None
        else os.environ.get("SHOOTER_VOD_INTERVAL_GAP_SEC", str(interval_gap_sec()))
    )
    parts = [f"{max(0.0, lo - pad)}-{hi + pad}" for lo, hi in sent_intervals]
    return ";".join(parts)


def peak_in_exclude_intervals(
    peak: float,
    intervals: list[tuple[float, float]] | None = None,
) -> bool:
    blocks = intervals if intervals is not None else parse_exclude_intervals()
    for lo, hi in blocks:
        if lo <= peak <= hi:
            return True
    return False


def filter_starts_outside_sent(
    starts: list[float],
    intervals: list[tuple[float, float]] | None = None,
) -> list[float]:
    blocks = intervals if intervals is not None else parse_exclude_intervals()
    if not blocks:
        return starts
    return [s for s in starts if not peak_in_exclude_intervals(s, blocks)]


def used_intervals_for_shooter_vod(
    vod_id: str,
    blocked_ids: set[str],
    index_segments: list[dict],
    *,
    vod_path: Path | None = None,
) -> list[tuple[float, float]]:
    """Reserved [start,end] for sent/labeled shooter segments — blocks duplicate fights."""
    fallback = float(os.environ.get("PUBG_FIGHT_MAX_SEC", "45"))
    intervals: list[tuple[float, float]] = []
    seen_sids: set[str] = set()

    for row in index_segments:
        row_vid = str(row.get("vod_id") or "")
        row_vod = str(row.get("vod") or "")
        if row_vid != vod_id:
            if not vod_path or (vod_path.name not in row_vod and str(vod_path) not in row_vod):
                continue
        sid = str(row.get("segment_id") or "")
        if not sid or sid not in blocked_ids:
            continue
        start = float(row.get("start", 0))
        peak = float(row.get("peak_start") or start)
        lead = float(os.environ.get("MLBB_VOD_LEAD_SEC", "4"))
        tail = float(os.environ.get("PUBG_FIGHT_TAIL_PAD_SEC", "6"))
        dur = float(row.get("duration") or row.get("fight_dur") or 0)
        if dur <= 0:
            path = Path(str(row.get("path") or ""))
            if path.exists():
                try:
                    from mlbb_vod_segment_feed import _ffprobe_duration

                    dur = float(_ffprobe_duration(path) or 0)
                except Exception:
                    dur = 0.0
        if dur <= 0:
            dur = fallback
        lo = min(start, max(0.0, peak - lead))
        hi = max(start + dur, peak + tail)
        intervals.append((lo, hi))
        seen_sids.add(sid)

    for sid in blocked_ids:
        if not sid.startswith(f"{vod_id}_") or sid in seen_sids:
            continue
        try:
            start = float(sid.rsplit("_", 1)[1])
        except ValueError:
            continue
        intervals.append((start, start + fallback))

    return intervals


def shooter_min_clip_sep_sec() -> float:
    return float(os.environ.get("SHOOTER_VOD_MIN_CLIP_SEP_SEC", "30"))


def shooter_interval_blocked(
    start: float,
    end: float,
    reserved_intervals: list[tuple[float, float]],
) -> bool:
    gap = float(os.environ.get("SHOOTER_VOD_INTERVAL_GAP_SEC", str(interval_gap_sec())))
    if conflicts_any_interval(start, end, reserved_intervals, gap=gap):
        return True
    # Clip earlier on timeline but too close before an already-sent/labeled window.
    min_sep = shooter_min_clip_sep_sec()
    for lo, hi in reserved_intervals:
        if end <= lo + gap and (lo - end) < min_sep:
            return True
    return False


def fight_intervals_from_entry(entry: dict[str, Any] | None) -> list[tuple[float, float]]:
    if not entry:
        return []
    out: list[tuple[float, float]] = []
    for row in entry.get("fight_intervals") or []:
        try:
            lo, hi = float(row[0]), float(row[1])
        except (TypeError, ValueError, IndexError):
            continue
        if hi > lo:
            out.append((lo, hi))
    return out


def record_fight_interval(entry: dict[str, Any] | None, start: float, end: float) -> None:
    if entry is None or end <= start:
        return
    rows = entry.setdefault("fight_intervals", [])
    lo, hi = round(start, 1), round(end, 1)
    for row in rows:
        try:
            if abs(float(row[0]) - lo) < 0.5 and abs(float(row[1]) - hi) < 0.5:
                return
        except (TypeError, ValueError, IndexError):
            continue
    rows.append([lo, hi])
    if len(rows) > 32:
        entry["fight_intervals"] = rows[-32:]
