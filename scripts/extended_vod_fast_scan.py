#!/usr/bin/env python3
"""Fast preflight for Genshin/WoT VODs — sparse probes before full highlight."""

from __future__ import annotations

import os
from pathlib import Path


def _probe_offsets(duration: float, *, skip_intro: float, max_probes: int) -> list[float]:
    dur = max(0.0, float(duration))
    if dur < skip_intro + 60:
        return []
    offsets: list[float] = []
    for delta in (0, 90, 180, 360, 540, 720):
        t = skip_intro + delta
        if t + 12 < dur - 30:
            offsets.append(round(t, 1))
    return offsets[:max_probes]


def genshin_fast_boss_check(video_path: Path) -> tuple[bool, str, list[float]]:
    """Sparse boss HP bar probe (4-6 windows)."""
    if os.environ.get("EXTENDED_VOD_FAST_PROBE", "1") != "1":
        return True, "fast_probe_disabled", []
    from smart_video_editor import ffprobe_duration

    dur = ffprobe_duration(video_path)
    if dur <= 0:
        return False, "fast_probe_no_duration", []
    skip = float(os.environ.get("GENSHIN_FAST_SKIP_INTRO", "90"))
    offsets = _probe_offsets(dur, skip_intro=skip, max_probes=6)
    if not offsets:
        return False, "fast_probe_too_short", []
    min_bar = float(os.environ.get("GENSHIN_FAST_MIN_BOSS_BAR", "0.12"))
    hits: list[float] = []
    top = 0.0
    try:
        from gameplay_gate import score_segment_combat

        for t in offsets:
            row = score_segment_combat(video_path, t, 12.0)
            bar = float(row[1] if isinstance(row, tuple) else row.get("boss_bar", 0))
            top = max(top, bar)
            if bar >= min_bar:
                hits.append(t)
    except Exception as exc:
        return False, f"fast_probe_exc={str(exc)[:60]}", []
    if not hits:
        return False, f"fast_boss_0/{len(offsets)} top={top:.3f}", []
    return True, f"fast_boss_{len(hits)}/{len(offsets)} top={top:.3f}", hits


def wot_fast_impact_check(video_path: Path) -> tuple[bool, str, list[float]]:
    """Sparse gun/impact audio probe."""
    if os.environ.get("EXTENDED_VOD_FAST_PROBE", "1") != "1":
        return True, "fast_probe_disabled", []
    from highlight_scorer import WINDOW_SEC, score_panns_audio
    from smart_video_editor import ffprobe_duration

    dur = ffprobe_duration(video_path)
    if dur <= 0:
        return False, "fast_probe_no_duration", []
    skip = float(os.environ.get("WOT_FAST_SKIP_INTRO", "60"))
    offsets = _probe_offsets(dur, skip_intro=skip, max_probes=6)
    if not offsets:
        return False, "fast_probe_too_short", []
    min_gun = float(os.environ.get("WOT_FAST_PANN_MIN", "0.14"))
    hits: list[float] = []
    top = 0.0
    for t in offsets:
        panns = score_panns_audio(video_path, t, WINDOW_SEC)
        gmax = float(panns.get("panns_gun_max", 0))
        top = max(top, gmax)
        if gmax >= min_gun:
            hits.append(t)
    if not hits:
        return False, f"fast_impact_0/{len(offsets)} top={top:.3f}", []
    return True, f"fast_impact_{len(hits)}/{len(offsets)} top={top:.3f}", hits
