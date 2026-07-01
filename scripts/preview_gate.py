#!/usr/bin/env python3
"""Mandatory validation before owner preview — think first, send second."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from strict_segment_gate import GAME_LABELS, normalize_profile
from visual_action_check import verify_segments_visual

log = logging.getLogger("preview_gate")


def _segment_duration(cand: dict) -> float:
    return float(cand.get("input_duration") or cand.get("output_duration") or 10.0)


def _gate_window(cand: dict, profile: str) -> tuple[float, float]:
    """Score combat at segment anchor; extra padding is render-only."""
    from highlight_scorer import WINDOW_SEC

    full_start = float(cand["start"])
    full_dur = _segment_duration(cand)
    prof = normalize_profile(profile)
    if prof not in ("pubg", "standoff") or full_dur <= WINDOW_SEC:
        return full_start, full_dur
    return full_start, WINDOW_SEC


def rescore_clip(
    video_path: Path,
    profile: str,
    cand: dict,
) -> tuple[bool, str, dict[str, Any]]:
    """Re-score one clip on its final (start, duration) — after padding/trim."""
    from highlight_scorer import score_candidate_window

    profile = normalize_profile(profile)
    start, duration = _gate_window(cand, profile)
    metrics = score_candidate_window(video_path, start, duration, profile)
    metrics.start = float(cand["start"])
    metrics.duration = _segment_duration(cand)

    if not metrics.rule_pass:
        return False, metrics.pass_reason or "rule_fail", metrics.to_dict()
    if not metrics.visual_pass:
        return False, metrics.pass_reason or "visual_fail", metrics.to_dict()
    prof = normalize_profile(profile)
    hook_min = float(os.environ.get("VIRAL_SEGMENT_HOOK_MIN", "0.35"))
    if prof == "mobile_legends" and metrics.rule_pass and metrics.visual_pass:
        # MOBA fights lack gunfire transients; trust HUD + CLIP + owner labels.
        hook_min = float(os.environ.get("VIRAL_MLBB_HOOK_MIN", "0.06"))
    combat_ok = prof in ("pubg", "standoff") and metrics.panns_gun_max >= 0.25
    mlbb_ok = (
        prof == "mobile_legends"
        and metrics.rule_pass
        and metrics.visual_pass
        and metrics.clip_score >= float(os.environ.get("VIRAL_MLBB_CLIP_HOOK_MIN", "0.12"))
    )
    if not combat_ok and not mlbb_ok and metrics.hook_score < hook_min:
        return False, f"hook_low={metrics.hook_score:.3f}", metrics.to_dict()

    if profile in ("pubg", "standoff"):
        from pubg_combat_gate import pubg_passes_combat_gate

        ok, reason, combat_row = pubg_passes_combat_gate(
            video_path, start, duration, profile, metrics=metrics
        )
        row = metrics.to_dict()
        row.update(combat_row)
        if not ok:
            return False, reason, row
        row["pass_reason"] = reason
        row["pass"] = True
        return True, reason, row

    row = metrics.to_dict()
    row["pass"] = True
    return True, metrics.pass_reason, row


def rescore_clips(video_path: Path, profile: str, clips: list[dict]) -> tuple[list[dict], bool, str]:
    """Re-score every selected clip; update highlight_metrics in place."""
    updated: list[dict] = []
    for cand in clips:
        ok, reason, hm = rescore_clip(video_path, profile, cand)
        if not ok:
            return [], False, f"seg@{cand.get('start')}:{reason}"
        fresh = dict(cand)
        fresh["highlight_metrics"] = hm
        fresh["strict_metrics"] = hm
        fresh["gate_reason"] = hm.get("pass_reason", "")
        updated.append(fresh)
    return updated, True, ""


def validate_clips_before_preview(
    video_path: Path,
    profile: str,
    clips: list[dict],
) -> tuple[bool, str, list[dict], list[dict], list[dict]]:
    """
    All gates for owner preview.
    Returns (ok, reason, rescored_clips, metrics_rows, visual_rows).
    """
    if not clips:
        return False, "no_clips", [], [], []

    profile = normalize_profile(profile)
    rescored, ok, reason = rescore_clips(video_path, profile, clips)
    if not ok:
        log.error("REFUSED preview: rescore failed %s", reason)
        return False, reason, [], [], []

    segment_pairs = [_gate_window(c, profile) for c in rescored]
    metrics_rows = [c.get("highlight_metrics") or c.get("strict_metrics") or {} for c in rescored]

    vis_passed, vis_total, visual_rows, vis_reason = verify_segments_visual(
        video_path, profile, segment_pairs, segment_metrics=metrics_rows
    )
    if vis_passed < vis_total:
        return False, vis_reason, [], metrics_rows, visual_rows

    from viral_scorer import montage_viral_score, segment_hook_ok

    _, hook_ok = montage_viral_score(rescored)
    if not hook_ok:
        return False, "viral_hook_fail_segment1", [], metrics_rows, visual_rows
    for idx, cand in enumerate(rescored, 1):
        hm = cand.get("highlight_metrics") or {}
        if not segment_hook_ok(hm):
            return False, f"viral_hook_fail_seg{idx}", [], metrics_rows, visual_rows

    game = GAME_LABELS.get(profile, profile)
    log.info(
        "preview_gate PASS game=%s segments=%s visual=%s/%s",
        game,
        len(rescored),
        vis_passed,
        vis_total,
    )
    return True, "", rescored, metrics_rows, visual_rows


def format_acceptance_table(profile: str, metrics_rows: list[dict]) -> str:
    game = GAME_LABELS.get(normalize_profile(profile), profile)
    header = "| # | start | panns | gunfire | burst | clip | visual | hook | PASS |"
    sep = "|---|-------|-------|---------|-------|------|--------|------|------|"
    lines = [header, sep]
    for i, row in enumerate(metrics_rows, 1):
        lines.append(
            f"| {i} | {row.get('start', 0)} | {row.get('panns_gun_max', 0):.3f} | "
            f"{row.get('gunfire_density', '—')} | {row.get('burst_ratio', '—')} | "
            f"{row.get('clip_score', 0):.3f} | {row.get('visual_pass', False)} | "
            f"{row.get('hook_score', 0):.3f} | {row.get('pass', row.get('rule_pass', False))} |"
        )
    return f"{game}\n" + "\n".join(lines)
