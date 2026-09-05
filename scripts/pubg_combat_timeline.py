#!/usr/bin/env python3
"""Combat timeline for PUBG VODs: multi-signal events without a fixed scene cap.

INVARIANT (do not break in PR #94 or follow-ups):
  * timeline / combat_score  →  RANK candidates only
  * hard gates below          →  ALLOW or BLOCK Telegram send
      - PANNs gun threshold
      - shooting gate
      - visual combat gate
      - loot/walk reject
      - bot-farm reject
      - POV engagement
      - rendered MP4 presend
      - early_hook on RENDERED mp4 @ 0.0–2.0s (never source VOD seek)
  * Never replace hard gates with a single combat_score.

Pipeline shape (cheap → strict):
  Stage A: dense/audio seeds across the FULL VOD (budget scales with duration)
  Stage B: merge neighboring positives into combat CLUSTERS (one clip / fight)
  Gates:   burst clusters + negative (menu/loot/run) penalties
  Presend: early-action shift validated on RENDERED clip (+0/+1/+2/+3s)
  Killfeed: score bonus only — never the sole candidate source

Rollout:
  PUBG_COMBAT_TIMELINE=1            enable ranking/clustering
  PUBG_COMBAT_TIMELINE_ENFORCE=0    shadow mode (default) — log only
  PUBG_COMBAT_TIMELINE_ENFORCE=1    enforce cluster/budget shortlist caps
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence


# ---------------------------------------------------------------------------
# Machine-readable reject reasons (presend_report / ledger / shadow JSONL)
# ---------------------------------------------------------------------------
REASON_TIMELINE_NO_COMBAT_ZONE = "timeline_no_combat_zone"
REASON_TIMELINE_SHORT_EVENT = "timeline_short_event"
REASON_TIMELINE_RUN_BEFORE_ACTION = "timeline_run_before_action"
REASON_TIMELINE_MENU_DETECTED = "timeline_menu_detected"
REASON_EARLY_HOOK_LOW = "early_hook_low"
REASON_SHIFT_RENDER_ALL_FAILED = "shift_render_all_failed"
REASON_CLUSTER_DUPLICATE = "cluster_duplicate"
REASON_PRESEND_VISUAL_FAIL = "presend_visual_fail"
REASON_PRESEND_LOOT_WALK = "presend_loot_walk"
REASON_PRESEND_BOT_FARM = "presend_bot_farm"
REASON_PRESEND_NO_DURATION = "presend_no_duration"
REASON_COST_BUDGET_EXCEEDED = "timeline_cost_budget_exceeded"


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
    gunfire_seconds: float = 0.0
    killfeed_hits: int = 0
    early_hook_score: float = 0.0
    reasons: list[str] = field(default_factory=list)

    @property
    def duration(self) -> float:
        return max(0.0, float(self.end) - float(self.start))

    def to_cluster_dict(self) -> dict[str, Any]:
        return {
            "combat_start": round(float(self.start), 3),
            "combat_peak": round(float(self.peak), 3),
            "combat_end": round(float(self.end), 3),
            "score": round(float(self.score), 4),
            "gunfire_seconds": round(float(self.gunfire_seconds), 3),
            "killfeed_hits": int(self.killfeed_hits),
            "early_hook_score": round(float(self.early_hook_score), 4),
            "reasons": list(self.reasons),
        }


def timeline_enabled() -> bool:
    return os.environ.get("PUBG_COMBAT_TIMELINE", "1") == "1"


def timeline_enforce_enabled() -> bool:
    """When false (default), timeline only shadows/ranks — never sole send reject."""
    return os.environ.get("PUBG_COMBAT_TIMELINE_ENFORCE", "0") == "1"


def timeline_cannot_authorize_send(timeline_score: float | None = None) -> bool:
    """Hard invariant: timeline/combat score must never authorize Telegram send.

    Always returns True (= hard gates still required). Call sites may assert this.
    """
    _ = timeline_score
    return True


def hard_gates_required() -> tuple[str, ...]:
    return (
        "panns_gun_threshold",
        "shooting_gate",
        "visual_combat_gate",
        "loot_walk_reject",
        "bot_farm_reject",
        "pov_engagement",
        "rendered_mp4_presend",
        "early_hook_rendered_0_2s",
    )


@dataclass(frozen=True)
class TimelineCostLimits:
    max_zones: int = 20
    max_dense_seconds: float = 240.0
    ocr_top_n: int = 12
    render_top_n: int = 2
    early_hook_max_shift_attempts: int = 3
    cluster_gap_sec: float = 6.0


def timeline_cost_limits() -> TimelineCostLimits:
    return TimelineCostLimits(
        max_zones=int(os.environ.get("PUBG_TIMELINE_MAX_ZONES", "20")),
        max_dense_seconds=float(os.environ.get("PUBG_TIMELINE_MAX_DENSE_SECONDS", "240")),
        ocr_top_n=int(os.environ.get("PUBG_TIMELINE_OCR_TOP_N", "12")),
        render_top_n=int(os.environ.get("PUBG_TIMELINE_RENDER_TOP_N", "2")),
        early_hook_max_shift_attempts=int(
            os.environ.get("PUBG_EARLY_HOOK_MAX_SHIFT_ATTEMPTS", "3")
        ),
        cluster_gap_sec=float(os.environ.get("PUBG_TIMELINE_CLUSTER_GAP_SEC", "6")),
    )


def adaptive_event_budget(duration_sec: float) -> int:
    """How many combat events to keep — scales with VOD length, no tiny fixed cap.

    Long VODs must still surface fights near the end; a hard top-3 would drop them.
    Soft ceiling exists only as a safety valve for pathological 8h dumps.
    PUBG_TIMELINE_MAX_ZONES caps dense/OCR/render shortlists (enforce path), not
    this discovery budget.
    """
    dur = max(0.0, float(duration_sec))
    per_sec = float(os.environ.get("PUBG_COMBAT_EVENT_PER_SEC", "75"))
    floor = int(os.environ.get("PUBG_COMBAT_EVENT_BUDGET_MIN", "6"))
    ceiling = int(os.environ.get("PUBG_COMBAT_EVENT_BUDGET_MAX", "96"))
    raw = int(dur / max(30.0, per_sec)) + floor
    return max(floor, min(ceiling, raw))



def adaptive_candidate_pool(duration_sec: float, *, min_clips: int = 2) -> int:
    """Recall pool for peak discovery — grows with duration so the tail is scanned."""
    base = adaptive_event_budget(duration_sec)
    return max(
        int(min_clips) * 4,
        base,
        int(os.environ.get("SHOOTER_VOD_CANDIDATE_POOL_TARGET", "16")),
    )


def dense_scan_span_for_duration(duration_sec: float, skip: float) -> float:
    """Prefer full-VOD Stage-A coverage; PCM hard cap is soft/duration-aware.

    PUBG_TIMELINE_MAX_DENSE_SECONDS bounds *dense re-scan around top zones*
    (see `dense_zone_budget_seconds`), NOT the cheap full-VOD seed span.
    """
    body = max(0.0, float(duration_sec) - float(skip) - 12.0)
    hard_cap = float(os.environ.get("SHOOTER_VOD_DENSE_PCM_MAX_SEC", "0") or 0)
    if hard_cap > 0:
        return min(body, max(600.0, hard_cap))
    return body


def dense_zone_budget_seconds() -> float:
    """Max seconds of expensive dense re-scan across top zones (cost limit)."""
    return float(timeline_cost_limits().max_dense_seconds)

    body = max(0.0, float(duration_sec) - float(skip) - 12.0)
    hard_cap = float(os.environ.get("SHOOTER_VOD_DENSE_PCM_MAX_SEC", "0") or 0)
    dense_cap = float(timeline_cost_limits().max_dense_seconds)
    capped = body
    if hard_cap > 0:
        capped = min(capped, max(600.0, hard_cap))
    # Soft cost hint: prefer not exceeding max_dense_seconds for Stage-A when set,
    # but never shrink below 600s on long VODs (tail coverage still required).
    if dense_cap > 0 and body > dense_cap:
        capped = min(capped, max(dense_cap, min(body, max(600.0, dense_cap))))
    return capped


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
    """Weighted combat score for RANKING only; killfeed is a bonus, never required.

    This score must never authorize a Telegram send by itself.
    """
    assert timeline_cannot_authorize_send()
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
    """Merge neighboring positives into combat CLUSTERS — one segment per fight.

    Peaks closer than cluster gap collapse into one event with
    combat_start / combat_peak / combat_end so we do not re-render
    120/122/124/128/131 of the same fight.
    """
    if not points:
        return []
    pos_min = float(
        positive_min
        if positive_min is not None
        else os.environ.get("PUBG_COMBAT_TIMELINE_POSITIVE_MIN", "0.18")
    )
    limits = timeline_cost_limits()
    gap = float(
        merge_gap_sec
        if merge_gap_sec is not None
        else os.environ.get(
            "PUBG_COMBAT_EVENT_MERGE_GAP_SEC", str(limits.cluster_gap_sec)
        )
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
        for point in cluster:
            if point.gunfire >= pos_min * 0.85 or point.combat >= pos_min:
                start = float(point.t)
                break
        end = min(float(duration_sec), float(cluster[-1].t) + tail)
        if end - start < min_dur:
            continue
        max_gun = max(p.gunfire for p in cluster)
        max_eng = max(p.engagement for p in cluster)
        max_hit = max(p.hit_flash for p in cluster)
        max_kf = max(p.killfeed for p in cluster)
        if max_gun < pos_min * 0.5 and max_eng < 0.2 and max_hit < 0.2:
            continue
        max_menu = max(p.menu_penalty for p in cluster)
        max_loot = max(p.loot_run_penalty for p in cluster)
        reasons: list[str] = ["timeline_merge"]
        if max_menu >= 0.55 and max_gun < 0.35:
            continue
        if max_loot >= 0.55 and max_gun < 0.30:
            continue
        gunfire_seconds = float(sum(1 for p in cluster if p.gunfire >= pos_min * 0.5))
        killfeed_hits = int(sum(1 for p in cluster if p.killfeed >= 0.35))
        score = sum(max(0.0, p.combat) for p in cluster) / max(len(cluster), 1)
        score += max_kf * 0.15
        events.append(
            CombatEvent(
                start=round(start, 2),
                peak=round(float(peak_pt.t), 2),
                end=round(end, 2),
                score=round(score, 4),
                killfeed_bonus=round(max_kf, 3),
                gunfire_seconds=round(gunfire_seconds, 2),
                killfeed_hits=killfeed_hits,
                reasons=reasons,
            )
        )

    events.sort(key=lambda e: -e.score)
    budget = adaptive_event_budget(duration_sec)
    kept = events[:budget]
    # Extra dedupe pass so near-duplicate peaks collapse to one cluster.
    deduped: list[CombatEvent] = []
    for event in sorted(kept, key=lambda e: e.peak):
        if deduped and abs(event.peak - deduped[-1].peak) < gap:
            if event.score > deduped[-1].score:
                event.reasons = list(event.reasons) + [REASON_CLUSTER_DUPLICATE]
                deduped[-1] = event
            continue
        deduped.append(event)
    return deduped


def burst_cluster_ok(
    *,
    clusters: int,
    quarters_active: int,
    active_sec: float,
    has_visual: bool = False,
) -> tuple[bool, str]:
    """Reject one-shot / single-burst false peaks."""
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
    """Candidate starts for shift attempts (source hint OR rendered loop)."""
    limits = timeline_cost_limits()
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
        if len(out) >= max(1, limits.early_hook_max_shift_attempts + 1):
            break
    return out or [round(float(start_sec), 2)]


def pick_early_action_start(
    start_sec: float,
    window_scores: dict[float, float],
    *,
    min_score: float | None = None,
) -> tuple[float, float, str]:
    """Pick the shift with the best first-window combat score (source HINT only).

    For send authorization use `pick_early_hook_on_rendered`, which scores the
    RENDERED mp4 at 0.0–2.0s after each shift render + full presend.
    """
    floor = float(
        min_score
        if min_score is not None
        else os.environ.get("PUBG_EARLY_ACTION_MIN_SCORE", "0.20")
    )
    best_start = float(start_sec)
    best_score = float(
        window_scores.get(round(best_start, 2), window_scores.get(best_start, -1.0))
    )
    reason = "early_action_keep"
    for cand in early_action_start_candidates(start_sec):
        score = float(window_scores.get(round(cand, 2), window_scores.get(cand, -1.0)))
        if score > best_score:
            best_start, best_score, reason = (
                cand,
                score,
                f"early_action_shift={cand - start_sec:.0f}s",
            )
    if best_score < floor:
        return float(start_sec), best_score, f"early_action_weak={best_score:.3f}"
    return best_start, best_score, reason


def score_early_hook_on_rendered(
    rendered_path: Path | str,
    *,
    window_sec: float | None = None,
) -> tuple[float, str, dict[str, Any]]:
    """Measure early-hook on RENDERED mp4 at 0.0–2.0s — never on source VOD."""
    from clip_hook_gate import hook_gate_clip

    window = float(
        window_sec
        if window_sec is not None
        else os.environ.get("PUBG_EARLY_HOOK_WINDOW_SEC", "2.0")
    )
    ok, reason, report = hook_gate_clip(rendered_path, window_sec=window)
    report = dict(report or {})
    report["early_hook_on"] = "rendered_mp4"
    report["early_hook_window_sec"] = window
    max_rms = float(report.get("max_rms") or 0.0)
    y_delta = float(report.get("y_delta") or 0.0)
    max_menu = float(report.get("max_menu") or 0.0)
    score = max(
        0.0,
        min(1.0, max_rms * 0.65 + min(1.0, y_delta / 12.0) * 0.35 - max_menu * 0.4),
    )
    report["early_hook_score"] = round(score, 4)
    if not ok:
        tagged = reason if str(reason).startswith("hook_") else f"{REASON_EARLY_HOOK_LOW}:{reason}"
        return score, tagged, report
    floor = float(os.environ.get("PUBG_EARLY_HOOK_MIN_SCORE", "0.18"))
    if score < floor:
        return score, f"{REASON_EARLY_HOOK_LOW}={score:.3f}<{floor:.2f}", report
    return score, "early_hook_ok", report


def pick_early_hook_on_rendered(
    base_start_sec: float,
    *,
    render_fn: Callable[[float], Path | str | None],
    presend_fn: Callable[[float, Path], tuple[bool, str, dict[str, Any]]] | None = None,
) -> tuple[float | None, Path | None, dict[str, Any]]:
    """Shift-render loop: render → hard presend → early-hook on rendered 0–2s.

    If every attempt fails, return reason=shift_render_all_failed (do not send).
    """
    attempts: list[dict[str, Any]] = []
    best_fail: dict[str, Any] | None = None
    for cand in early_action_start_candidates(base_start_sec):
        rendered = render_fn(float(cand))
        if rendered is None:
            attempts.append({"start": cand, "reason": "render_failed"})
            continue
        path = Path(rendered)
        if presend_fn is not None:
            ok, reason, report = presend_fn(float(cand), path)
            entry: dict[str, Any] = {
                "start": cand,
                "presend_ok": bool(ok),
                "presend_reason": reason,
                "presend_report": report,
            }
            if not ok:
                attempts.append(entry)
                best_fail = entry
                continue
        hook_score, hook_reason, hook_report = score_early_hook_on_rendered(path)
        entry = {
            "start": cand,
            "presend_ok": True,
            "early_hook_score": hook_score,
            "early_hook_reason": hook_reason,
            "early_hook_report": hook_report,
            "rendered": str(path),
        }
        attempts.append(entry)
        if hook_reason == "early_hook_ok":
            return float(cand), path, {"ok": True, "attempts": attempts, **entry}
        best_fail = entry
    return None, None, {
        "ok": False,
        "reason": REASON_SHIFT_RENDER_ALL_FAILED,
        "attempts": attempts,
        "last": best_fail,
    }


def events_to_peaks(events: Sequence[CombatEvent]) -> list[float]:
    return [float(e.peak) for e in events]


def refine_peaks_with_timeline(
    peaks: Sequence[float],
    *,
    duration_sec: float,
    scores: Sequence[float] | None = None,
) -> list[float]:
    """Turn Stage-A peaks into duration-scaled combat CLUSTERS → one peak each.

    Ranking only — does not authorize send. Asserts the hard-gate invariant.
    """
    assert timeline_cannot_authorize_send()
    if not timeline_enabled():
        return [float(p) for p in peaks]
    points = points_from_gun_peaks(peaks, scores=scores)
    events = merge_combat_events(points, duration_sec=duration_sec)
    if not events:
        ordered = sorted(float(p) for p in peaks)
        return ordered[: adaptive_event_budget(duration_sec)]
    peaks_out = events_to_peaks(events)
    if timeline_enforce_enabled():
        peaks_out = peaks_out[: max(1, timeline_cost_limits().max_zones)]
    return peaks_out


def summarize_events(events: Iterable[CombatEvent]) -> dict[str, Any]:
    rows = list(events)
    return {
        "n": len(rows),
        "span": (
            [round(rows[0].start, 1), round(rows[-1].end, 1)] if rows else None
        ),
        "peaks": [round(e.peak, 1) for e in rows[:12]],
        "scores": [round(e.score, 3) for e in rows[:12]],
        "clusters": [e.to_cluster_dict() for e in rows[:12]],
        "enforce": timeline_enforce_enabled(),
        "hard_gates_required": list(hard_gates_required()),
        "cost_limits": asdict(timeline_cost_limits()),
    }


def append_timeline_shadow_report(path: Path | str, payload: dict[str, Any]) -> None:
    """Append-only JSONL shadow log for Phase A rollout (no send impact)."""
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    row = {"ts": time.time(), "enforce": timeline_enforce_enabled(), **payload}
    with out.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
