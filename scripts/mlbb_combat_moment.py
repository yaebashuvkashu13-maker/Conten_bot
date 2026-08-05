#!/usr/bin/env python3
"""Combat-first MLBB moment detection — HUD motion/minimap/skills + audio, not OCR banners."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from highlight_scorer import WINDOW_SEC, normalize_profile


def moment_anchor_mode() -> str:
    """banner = visual kill-banner refs first | combat = HUD+motion | motion = fight bounds only."""
    return (os.environ.get("MLBB_MOMENT_ANCHOR") or "banner").strip().lower()


def banner_enrich_only() -> bool:
    """When True, kill-banner OCR/color may decorate captions but never blocks a clip."""
    # Banner-first mode must require a real streak — enrich-only would ship junk fights.
    default = "0" if moment_anchor_mode() == "banner" else "1"
    return os.environ.get("MLBB_BANNER_ENRICH_ONLY", default) == "1"


def combat_probe_min_score() -> float:
    return float(os.environ.get("MLBB_COMBAT_PROBE_MIN", "0.40"))


def combat_gate_min_score() -> float:
    return float(os.environ.get("MLBB_COMBAT_GATE_MIN", "0.42"))


def score_combat_moment(
    video_path: Path,
    start_sec: float,
    *,
    duration_sec: float = 10.0,
    analysis: dict[str, Any] | None = None,
) -> tuple[float, dict[str, float]]:
    """
    Unified combat score for a VOD timestamp.
    Blends cached motion/audio bins with live HUD combat (minimap + skill buttons).
    """
    from mlbb_teamfight_detector import score_teamfight_bins, score_teamfight_hud

    if analysis is None:
        from mlbb_fight_segment import _analysis_for

        analysis = _analysis_for(video_path)

    bin_score = score_teamfight_bins(analysis, start_sec)
    hud_score, mini, skill = score_teamfight_hud(video_path, start_sec, duration_sec)
    teamfight = hud_score * 0.58 + bin_score * 0.42

    announce = 0.0
    if os.environ.get("MLBB_COMBAT_USE_ANNOUNCE_COLOR", "1") == "1":
        try:
            from gameplay_gate import _read_frame_at
            from mlbb_kill_banner import _announce_color_score

            frame = _read_frame_at(video_path, start_sec + min(2.0, duration_sec * 0.25))
            if frame is not None:
                announce = min(1.0, float(_announce_color_score(frame)) / 0.12)
        except Exception:
            announce = 0.0

    combined = min(1.0, teamfight * 0.88 + announce * 0.12)
    return combined, {
        "teamfight": round(teamfight, 4),
        "hud": round(hud_score, 4),
        "bins": round(bin_score, 4),
        "minimap": round(mini, 4),
        "skill": round(skill, 4),
        "announce": round(announce, 4),
    }


def passes_combat_moment(score: float) -> bool:
    return score >= combat_probe_min_score()


def passes_combat_gate(score: float) -> bool:
    return score >= combat_gate_min_score()


def enrich_banner_meta(vod: Path, peak_sec: float, meta: dict[str, Any]) -> dict[str, Any]:
    """Optional quick banner hint for Telegram caption — never required for acceptance."""
    if os.environ.get("MLBB_VOD_KILL_BANNER", "1") != "1":
        return meta
    if not banner_enrich_only() and moment_anchor_mode() == "banner":
        return meta
    try:
        from mlbb_kill_banner import _min_tier, find_banner_near_peak

        hit = find_banner_near_peak(vod, peak_sec, quick=True)
        if hit is None:
            return meta
        if hit.tier < _min_tier():
            return meta
        out = dict(meta)
        out.update(
            {
                "anchor": "combat+banner",
                "banner_sec": hit.sec,
                "kill_banner": hit.label,
                "kill_banner_tier": hit.tier,
                "banner_text": hit.text,
                "banner_source": hit.source,
            }
        )
        return out
    except Exception:
        return meta


def probe_offsets(duration: float, *, skip_intro: float) -> list[float]:
    dur = max(0.0, float(duration))
    if dur < skip_intro + 90:
        return []
    offsets: list[float] = []
    for delta in (0, 120, 300, 540, 900, 1320):
        t = skip_intro + delta
        if t + WINDOW_SEC < dur - 45:
            offsets.append(round(t, 1))
    mid = skip_intro + max(0.0, (dur - skip_intro) * 0.42)
    if mid + WINDOW_SEC < dur - 45 and all(abs(mid - x) > 75 for x in offsets):
        offsets.append(round(mid, 1))
    cap = int(os.environ.get("MLBB_VOD_FAST_PROBE_MAX", "6"))
    return sorted(set(offsets))[:cap]


def fast_combat_probe(
    video_path: Path,
    profile: str = "mobile_legends",
) -> tuple[bool, str, list[float]]:
    """
    Sparse HUD combat probes — MOBA-appropriate replacement for PANNs gunfire preflight.
    """
    profile = normalize_profile(profile)
    from smart_video_editor import ffprobe_duration

    dur = ffprobe_duration(video_path)
    if dur <= 0:
        return False, "combat_probe_no_duration", []

    skip = float(os.environ.get("MLBB_VOD_FAST_SKIP_INTRO", "300"))
    offsets = probe_offsets(dur, skip_intro=skip)
    if not offsets:
        return False, "combat_probe_too_short", []

    from mlbb_fight_segment import _analysis_for

    analysis = _analysis_for(video_path)
    hits: list[tuple[float, float]] = []
    top = 0.0
    for t in offsets:
        score, detail = score_combat_moment(video_path, t, analysis=analysis)
        top = max(top, score)
        if passes_combat_moment(score):
            hits.append((score, t))

    if not hits:
        return (
            False,
            f"combat_probe_0/{len(offsets)} top={top:.3f} need>={combat_probe_min_score():.2f}",
            [],
        )
    hits.sort(key=lambda row: row[0], reverse=True)
    seeds = [t for _, t in hits[:8]]
    return (
        True,
        f"combat_probe_{len(hits)}/{len(offsets)} top={top:.3f}",
        seeds,
    )
