#!/usr/bin/env python3
"""Combat timeline for PUBG VODs: multi-signal events without a fixed scene cap.

Pipeline shape (cheap → strict):
  Stage A: dense/audio seeds across the FULL VOD (budget scales with duration)
  Stage B: merge neighboring positives into combat events with real start/peak/end
  Gates:   burst clusters + negative (menu/loot/run) penalties
  Presend: early-action start shift (+0/+1/+2/+3s) so clips don't open on run-up
  Killfeed: score bonus only — never the sole candidate source
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence


@dataclass(frozen=True)
class TimelinePoint:
    t: float
    combat: float
    gunfire: float = 0.0
    killfeed: float = 0.0
    hit_flash: float = 0.0
    engagement: float = 0.0
    menu_penalty: float = 0.0
    loot_run_penalty: float = 0.0
    afk_penalty: float = 0.0
    bot_farm_penalty: float = 0.0


@dataclass
class CombatEvent:
    start: float
    peak: float
    end: float
    score: float
    burst_clusters: int = 0
    quarters_active: int = 0
    killfeed_bonus: float = 0.0
    reasons: list[str] = field(default_factory=list)

    @property
    def duration(self) -> float:
        return max(0.0, float(self.end) - float(self.start))


def timeline_enabled() -> bool:
    return os.environ.get("PUBG_COMBAT_TIMELINE", "1") == "1"


def adaptive_event_budget(duration_sec: float) -> int:
    """How many combat events to keep — scales with VOD length, no tiny fixed cap.

    Long VODs must still surface fights near the end; a hard top-3 would drop them.
    Soft ceiling exists only as a safety valve for pathological 8h dumps.
    """
    dur = max(0.0, float(duration_sec))
    # ~1 event per 75s of body, at least 6, soft max 96 (override via env).
    per_sec = float(os.environ.get("PUBG_COMBAT_EVENT_PER_SEC", "75"))
    floor = int(os.environ.get("PUBG_COMBAT_EVENT_BUDGET_MIN", "6"))
    ceiling = int(os.environ.get("PUBG_COMBAT_EVENT_BUDGET_MAX", "96"))
    raw = int(dur / max(30.0, per_sec)) + floor
    return max(floor, min(ceiling, raw))


def adaptive_candidate_pool(duration_sec: float, *, min_clips: int = 2) -> int:
    """Recall pool for peak discovery — grows with duration so the tail is scanned."""
    base = adaptive_event_budget(duration_sec)
    # Keep extra headroom for presend false-positive attrition.
    return max(int(min_clips) * 4, base, int(os.environ.get("SHOOTER_VOD_CANDIDATE_POOL_TARGET", "16")))


def dense_scan_span_for_duration(duration_sec: float, skip: float) -> float:
    """Prefer full-VOD coverage; PCM cap is soft and duration-aware."""
    body = max(0.0, float(duration_sec) - float(skip) - 12.0)
    hard_cap = float(os.environ.get("SHOOTER_VOD_DENSE_PCM_MAX_SEC", "0") or 0)
    if hard_cap > 0:
        return min(body, max(600.0, hard_cap))
    # Default: cover the whole body (chunked extract handled by callers).
    return body


def combat_score_point(
    *,
    gunfire: float = 0.0,
    killfeed: float = 0.0,
    hit_flash: float = 0.0,
    engagement: float = 0.0,
    menu_penalty: float = 0.0,
    loot_run_penalty: float = 0.0,
    afk_penalty: float = 0.0,
    bot_farm_penalty: float = 0.0,
) -> float:
    """Weighted combat score; killfeed is a bonus, never required."""
    kf_w = float(os.environ.get("PUBG_COMBAT_KILLFEED_WEIGHT", "0.55"))
    score = (
        float(gunfire)
        + float(killfeed) * kf_w
        + float(hit_flash) * 0.45
        + float(engagement) * 0.35
        - float(menu_penalty) * 1.25
        - float(loot_run_penalty) * 1.10
        - float(afk_penalty) * 1.50
        - float(bot_farm_penalty) * 1.20
    )
    return float(score)


def points_from_gun_peaks(
    peaks: Sequence[float],
    *,
    scores: Sequence[float] | None = None,
    half_window: float = 2.5,
) -> list[TimelinePoint]:
    """Build coarse timeline seeds from ranked gun peaks (Stage A output)."""
    out: list[TimelinePoint] = []
    for index, peak in enumerate(peaks):
        gun = 0.55
        if scores is not None and index < len(scores):
            gun = max(0.15, min(1.0, float(scores[index])))
        for delta in (-half_window, 0.0, half_window):
            t = max(0.0, float(peak) + delta)
            out.append(
                TimelinePoint(
                    t=round(t, 2),
                    combat=combat_score_point(gunfire=gun),
                    gunfire=gun,
                )
            )
    out.sort(key=lambda p: p.t)
    return out


def merge_combat_events(
    points: Sequence[TimelinePoint],
    *,
    duration_sec: float,
    positive_min: float | None = None,
    merge_gap_sec: float | None = None,
    min_event_sec: float | None = None,
    tail_pad_sec: float | None = None,
) -> list[CombatEvent]:
    """Merge neighboring positive samples into combat events with real bounds."""
    if not points:
        return []
    pos_min = float(
        positive_min
        if positive_min is not None
        else os.environ.get("PUBG_COMBAT_TIMELINE_POSITIVE_MIN", "0.18")
    )
    gap = float(
        merge_gap_sec
        if merge_gap_sec is not None
        else os.environ.get("PUBG_COMBAT_EVENT_MERGE_GAP_SEC", "4.0")
    )
    min_dur = float(
        min_event_sec
        if min_event_sec is not None
        else os.environ.get("PUBG_COMBAT_EVENT_MIN_SEC", "3.0")
    )
    tail = float(
        tail_pad_sec
        if tail_pad_sec is not None
        else os.environ.get("PUBG_COMBAT_EVENT_TAIL_PAD_SEC", "3.0")
    )

    ordered = sorted(points, key=lambda p: p.t)
    clusters: list[list[TimelinePoint]] = []
    cur: list[TimelinePoint] = []
    for point in ordered:
        if point.combat < pos_min:
            if cur:
                clusters.append(cur)
                cur = []
            continue
        if cur and (point.t - cur[-1].t) > gap:
            clusters.append(cur)
            cur = [point]
        else:
            cur.append(point)
    if cur:
        clusters.append(cur)

    events: list[CombatEvent] = []
    for cluster in clusters:
        peak_pt = max(cluster, key=lambda p: (p.combat, p.gunfire, p.killfeed))
        start = float(cluster[0].t)
        # Start = first sustained gunfire, not a fixed lead before peak.
        for point in cluster:
            if point.gunfire >= pos_min * 0.85 or point.combat >= pos_min:
                start = float(point.t)
                break
        end = min(float(duration_sec), float(cluster[-1].t) + tail)
        if end - start < min_dur:
            continue
        # Require gunfire or engagement confirmation (killfeed alone is not enough).
        max_gun = max(p.gunfire for p in cluster)
        max_eng = max(p.engagement for p in cluster)
        max_hit = max(p.hit_flash for p in cluster)
        max_kf = max(p.killfeed for p in cluster)
        if max_gun < pos_min * 0.5 and max_eng < 0.2 and max_hit < 0.2:
            continue
        # Negative-dominant clusters (menu/loot) drop.
        max_menu = max(p.menu_penalty for p in cluster)
        max_loot = max(p.loot_run_penalty for p in cluster)
        if max_menu >= 0.55 and max_gun < 0.35:
            continue
        if max_loot >= 0.55 and max_gun < 0.30:
            continue
        score = sum(max(0.0, p.combat) for p in cluster) / max(len(cluster), 1)
        score += max_kf * 0.15  # killfeed bonus only
        events.append(
            CombatEvent(
                start=round(start, 2),
                peak=round(float(peak_pt.t), 2),
                end=round(end, 2),
                score=round(score, 4),
                killfeed_bonus=round(max_kf, 3),
                reasons=["timeline_merge"],
            )
        )

    events.sort(key=lambda e: -e.score)
    budget = adaptive_event_budget(duration_sec)
    # Keep chronological coverage: take top-N by score, then re-sort by time so
    # the feed walks the VOD from start→end instead of only the loudest middle.
    kept = events[:budget]
    kept.sort(key=lambda e: e.peak)
    return kept


def burst_cluster_ok(
    *,
    clusters: int,
    quarters_active: int,
    active_sec: float,
    has_visual: bool = False,
) -> tuple[bool, str]:
    """Variant 5: reject one-shot / single-burst false peaks."""
    min_clusters = int(os.environ.get("PUBG_COMBAT_MIN_BURST_CLUSTERS", "2"))
    min_quarters = int(os.environ.get("PUBG_COMBAT_MIN_ACTIVE_QUARTERS", "2"))
    min_active = float(os.environ.get("PUBG_COMBAT_MIN_ACTIVE_SEC", "2.5"))
    if clusters < min_clusters:
        return False, f"burst_clusters={clusters}<{min_clusters}"
    if quarters_active < min_quarters:
        return False, f"active_quarters={quarters_active}<{min_quarters}"
    if active_sec < min_active:
        return False, f"active_sec={active_sec:.1f}<{min_active}"
    if os.environ.get("PUBG_COMBAT_REQUIRE_VISUAL", "0") == "1" and not has_visual:
        return False, "missing_visual_confirm"
    return True, "burst_ok"


def early_action_start_candidates(start_sec: float) -> list[float]:
    """Variant 2: try original and +1/+2/+3s shifts."""
    shifts = os.environ.get("PUBG_EARLY_ACTION_SHIFTS_SEC", "0,1,2,3")
    out: list[float] = []
    for raw in shifts.split(","):
        raw = raw.strip()
        if not raw:
            continue
        try:
            delta = float(raw)
        except ValueError:
            continue
        out.append(round(max(0.0, float(start_sec) + delta), 2))
    return out or [round(float(start_sec), 2)]


def pick_early_action_start(
    start_sec: float,
    window_scores: dict[float, float],
    *,
    min_score: float | None = None,
) -> tuple[float, float, str]:
    """Pick the shift with the best first-window combat score."""
    floor = float(
        min_score
        if min_score is not None
        else os.environ.get("PUBG_EARLY_ACTION_MIN_SCORE", "0.20")
    )
    best_start = float(start_sec)
    best_score = float(window_scores.get(round(best_start, 2), window_scores.get(best_start, -1.0)))
    reason = "early_action_keep"
    for cand in early_action_start_candidates(start_sec):
        score = float(window_scores.get(round(cand, 2), window_scores.get(cand, -1.0)))
        if score > best_score:
            best_start, best_score, reason = cand, score, f"early_action_shift={cand - start_sec:.0f}s"
    if best_score < floor:
        return float(start_sec), best_score, f"early_action_weak={best_score:.3f}"
    return best_start, best_score, reason


def events_to_peaks(events: Sequence[CombatEvent]) -> list[float]:
    return [float(e.peak) for e in events]


def refine_peaks_with_timeline(
    peaks: Sequence[float],
    *,
    duration_sec: float,
    scores: Sequence[float] | None = None,
) -> list[float]:
    """Turn Stage-A peaks into duration-scaled combat events → peak list."""
    if not timeline_enabled():
        return [float(p) for p in peaks]
    points = points_from_gun_peaks(peaks, scores=scores)
    events = merge_combat_events(points, duration_sec=duration_sec)
    if not events:
        # Fallback: keep original peaks but still apply adaptive budget so the
        # tail is not truncated to a fixed top-3.
        ordered = sorted(float(p) for p in peaks)
        return ordered[: adaptive_event_budget(duration_sec)]
    return events_to_peaks(events)


def summarize_events(events: Iterable[CombatEvent]) -> dict[str, Any]:
    rows = list(events)
    return {
        "n": len(rows),
        "span": (
            [round(rows[0].start, 1), round(rows[-1].end, 1)] if rows else None
        ),
        "peaks": [round(e.peak, 1) for e in rows[:12]],
        "scores": [round(e.score, 3) for e in rows[:12]],
    }
