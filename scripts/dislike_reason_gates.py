#!/usr/bin/env python3
"""Per-dislike-reason gates: menu / loot-run / no-gun / bad-render / boring."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any


REASON_ALIASES: dict[str, str] = {
    "menu": "menu",
    "меню": "menu",
    "menu_lobby": "menu",
    "lobby": "menu",
    "menu_garage": "menu",
    "garage": "menu",
    "loot_run": "loot_run",
    "run": "loot_run",
    "running": "loot_run",
    "беготня": "loot_run",
    "explore": "loot_run",
    "no_gun": "no_gun",
    "no_shooting": "no_gun",
    "нет_стрельбы": "no_gun",
    "silent": "no_gun",
    "bad_render": "bad_render",
    "render": "bad_render",
    "blurry": "bad_render",
    "freeze": "bad_render",
    "boring": "boring",
    "скучно": "boring",
    "uninteresting": "boring",
}


@dataclass(frozen=True)
class ReasonThresholds:
    gun_density_min: float
    burst_ratio_min: float
    motion_max: float
    menu_overlay_max: float
    visual_min: float
    hook_gun_min: float
    hook_motion_min: float


# Separate floors per failure mode — never softens below BASE of game_adaptive_thresholds.
REASON_THRESHOLDS: dict[str, ReasonThresholds] = {
    "menu": ReasonThresholds(
        gun_density_min=0.085,
        burst_ratio_min=7.0,
        motion_max=0.14,
        menu_overlay_max=0.22,
        visual_min=0.35,
        hook_gun_min=0.04,
        hook_motion_min=0.04,
    ),
    "loot_run": ReasonThresholds(
        gun_density_min=0.090,
        burst_ratio_min=7.5,
        motion_max=0.12,
        menu_overlay_max=0.30,
        visual_min=0.32,
        hook_gun_min=0.05,
        hook_motion_min=0.035,
    ),
    "no_gun": ReasonThresholds(
        gun_density_min=0.095,
        burst_ratio_min=8.0,
        motion_max=0.20,
        menu_overlay_max=0.35,
        visual_min=0.30,
        hook_gun_min=0.06,
        hook_motion_min=0.02,
    ),
    "bad_render": ReasonThresholds(
        gun_density_min=0.070,
        burst_ratio_min=6.5,
        motion_max=0.18,
        menu_overlay_max=0.28,
        visual_min=0.45,
        hook_gun_min=0.03,
        hook_motion_min=0.03,
    ),
    "boring": ReasonThresholds(
        gun_density_min=0.080,
        burst_ratio_min=7.0,
        motion_max=0.16,
        menu_overlay_max=0.30,
        visual_min=0.40,
        hook_gun_min=0.045,
        hook_motion_min=0.035,
    ),
}


def normalize_reason(reason: str | None) -> str | None:
    if not reason:
        return None
    raw = str(reason).strip().lower().replace("-", "_").replace(" ", "_")
    if raw in REASON_ALIASES:
        return REASON_ALIASES[raw]
    for key, canon in REASON_ALIASES.items():
        if key in raw:
            return canon
    return None


def thresholds_for_reason(reason: str | None) -> ReasonThresholds | None:
    canon = normalize_reason(reason)
    if not canon:
        return None
    return REASON_THRESHOLDS.get(canon)


def _f(metrics: dict[str, Any], *keys: str, default: float = 0.0) -> float:
    for key in keys:
        val = metrics.get(key)
        if isinstance(val, (int, float)):
            return float(val)
    return float(default)


def evaluate_reason_gates(
    metrics: dict[str, Any] | None,
    *,
    active_reasons: list[str] | None = None,
) -> tuple[bool, str, dict[str, Any]]:
    """
    Apply the strictest of the reason-specific floors.
    active_reasons: optional list of recent owner dislike reasons to emphasize.
    If empty, still applies a mild union of loot_run+no_gun when env asks.
    """
    metrics = dict(metrics or {})
    reasons = [normalize_reason(r) for r in (active_reasons or [])]
    reasons = [r for r in reasons if r]
    if not reasons and os.environ.get("DISLIKE_REASON_GATES_DEFAULT", "1") == "1":
        # Always keep anti-menu + anti-loot floors on for PUBG-like feeds.
        reasons = ["menu", "loot_run"]

    chosen = [REASON_THRESHOLDS[r] for r in reasons if r in REASON_THRESHOLDS]
    if not chosen:
        return True, "ok", {"active_reasons": reasons}

    gun_min = max(t.gun_density_min for t in chosen)
    burst_min = max(t.burst_ratio_min for t in chosen)
    motion_max = min(t.motion_max for t in chosen)
    menu_max = min(t.menu_overlay_max for t in chosen)
    visual_min = max(t.visual_min for t in chosen)
    # Drought / ops may raise menu_overlay_max (HUD false positives) without
    # disabling the gate. Loot/gun floors stay at reason-table values.
    override = os.environ.get("DISLIKE_MENU_OVERLAY_MAX", "").strip()
    if override:
        try:
            menu_max = max(menu_max, float(override))
        except ValueError:
            pass

    gun = _f(metrics, "gun_density", "gunfire_density", "gunshot_density")
    burst = _f(metrics, "burst_ratio", "gun_burst_ratio")
    motion = _f(metrics, "center_motion", "motion", "run_motion")
    menu = _f(metrics, "menu_overlay", "overlay_text", "center_text")
    visual = _f(metrics, "visual", "visual_score", "clip_score", default=1.0)

    report = {
        "active_reasons": reasons,
        "floors": {
            "gun_density_min": gun_min,
            "burst_ratio_min": burst_min,
            "motion_max": motion_max,
            "menu_overlay_max": menu_max,
            "visual_min": visual_min,
        },
        "metrics": {
            "gun_density": gun,
            "burst_ratio": burst,
            "motion": motion,
            "menu_overlay": menu,
            "visual": visual,
        },
    }
    if menu >= menu_max and menu > 0:
        return False, f"reason_menu_overlay={menu:.3f}>={menu_max:.3f}", report
    if gun > 0 and gun < gun_min:
        return False, f"reason_low_gun={gun:.3f}<{gun_min:.3f}", report
    if burst > 0 and burst < burst_min:
        return False, f"reason_low_burst={burst:.2f}<{burst_min:.2f}", report
    if motion > 0 and gun < gun_min * 0.85 and motion > motion_max:
        return False, f"reason_loot_run=motion{motion:.3f}>gun{gun:.3f}", report
    if visual < visual_min:
        return False, f"reason_low_visual={visual:.3f}<{visual_min:.3f}", report
    return True, "ok", report


def recent_dislike_reasons(game: str, *, limit: int = 30) -> list[str]:
    """Pull recent bad-feedback reasons from the quality ledger."""
    try:
        from vod_clip_quality_ledger import iter_events
    except Exception:
        return []
    out: list[str] = []
    for row in reversed(iter_events(game)):
        if row.get("decision") != "feedback" or row.get("label") != "bad":
            continue
        canon = normalize_reason(str(row.get("reason") or ""))
        if canon:
            out.append(canon)
        if len(out) >= limit:
            break
    return out
