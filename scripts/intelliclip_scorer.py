#!/usr/bin/env python3
"""
Quso.ai Intelliclips-style moment ranking — multi-signal fusion without manual timestamps.

Signals (per 10s window):
  - audio_peak: gunfire/RMS energy from analyze_video bins
  - visual_dynamics: scene-change + center motion spikes
  - hook_energy: first ~3s of window (strong open like Shorts hooks)
  - event_density: sustained combat (not single noise blip)
  - speech_clarity: low music dominance (talking-head streams)

Cheap stage-1 uses bins only; stage-2 enriches after PANNs/CLIP in highlight_scorer.
"""

from __future__ import annotations

import logging
import os
from functools import lru_cache
from typing import Any

import numpy as np

log = logging.getLogger("intelliclip")

HOOK_SEC = float(os.environ.get("INTELLICLIP_HOOK_SEC", "3.0"))


def _normalize_profile(profile: str) -> str:
    p = profile.strip().lower()
    if p == "mlbb":
        return "mobile_legends"
    if p == "world_of_tanks":
        return "wot"
    return p


@lru_cache(maxsize=8)
def _load_analysis(video_key: str, video_path: str) -> dict[str, Any]:
    from smart_video_editor import analyze_video

    return analyze_video(__import__("pathlib").Path(video_path))


def load_analysis(video_path) -> dict[str, Any]:
    from pathlib import Path

    p = Path(video_path)
    return _load_analysis(str(p.resolve()), str(p.resolve()))


def _bin_range(analysis: dict, start: float, duration: float) -> tuple[int, int]:
    win = float(analysis.get("window_seconds", 2.0))
    i0 = max(0, int(start / win))
    i1 = max(i0 + 1, int((start + duration) / win))
    return i0, i1


def window_bin_signals(
    analysis: dict[str, Any],
    start: float,
    duration: float,
    profile: str,
) -> dict[str, float]:
    """Fast signals from precomputed analyze_video bins (no PANNs)."""
    profile = _normalize_profile(profile)
    win = float(analysis.get("window_seconds", 2.0))
    i0, i1 = _bin_range(analysis, start, duration)
    hook_i1 = max(i0 + 1, int((start + HOOK_SEC) / win))

    def slice_arr(key: str, alt: str | None = None) -> np.ndarray:
        raw = analysis.get(key)
        if raw is None and alt:
            raw = analysis.get(alt)
        if raw is None:
            return np.zeros(1, dtype=np.float32)
        arr = np.asarray(raw, dtype=np.float32)
        return arr[i0:i1] if i1 <= len(arr) else arr[i0:]

    audio = slice_arr("audio")
    gun = slice_arr("gunfire", "audio")
    motion = slice_arr("center_motion")
    scene = slice_arr("scene")
    hook_gun = np.asarray(analysis.get("gunfire", analysis.get("audio", [])), dtype=np.float32)
    hook_gun = hook_gun[i0:hook_i1] if hook_i1 <= len(hook_gun) else hook_gun[i0:]

    audio_peak = float(np.max(audio)) if audio.size else 0.0
    gun_peak = float(np.max(gun)) if gun.size else 0.0
    gun_mean = float(np.mean(gun)) if gun.size else 0.0
    motion_peak = float(np.max(motion)) if motion.size else 0.0
    motion_p90 = float(np.percentile(motion, 90)) if motion.size else 0.0
    scene_delta = float(np.max(scene)) if scene.size else 0.0
    hook_energy = float(np.max(hook_gun)) if hook_gun.size else gun_peak

    # Sustained action: high mean relative to peak (not one-frame spike)
    sustain = gun_mean / max(gun_peak, 1e-4) if gun_peak > 0.02 else 0.0

    return {
        "audio_peak": audio_peak,
        "gun_peak": gun_peak,
        "gun_mean": gun_mean,
        "gun_sustain": min(1.0, sustain),
        "motion_peak": motion_peak,
        "motion_p90": motion_p90,
        "scene_delta": scene_delta,
        "hook_energy": hook_energy,
        "visual_dynamics": min(1.0, motion_p90 * 1.4 + scene_delta * 0.8),
    }


def profile_weights(profile: str) -> dict[str, float]:
    profile = _normalize_profile(profile)
    if profile in ("pubg", "standoff"):
        return {
            "gun_peak": 0.32,
            "hook_energy": 0.18,
            "visual_dynamics": 0.22,
            "gun_sustain": 0.12,
            "audio_peak": 0.08,
            "motion_peak": 0.08,
        }
    if profile == "mobile_legends":
        return {
            "motion_peak": 0.30,
            "visual_dynamics": 0.28,
            "hook_energy": 0.15,
            "gun_peak": 0.12,
            "gun_sustain": 0.08,
            "audio_peak": 0.07,
        }
    if profile == "genshin":
        return {
            "visual_dynamics": 0.30,
            "motion_peak": 0.25,
            "hook_energy": 0.15,
            "scene_delta": 0.15,
            "audio_peak": 0.10,
            "gun_peak": 0.05,
        }
    if profile == "wot":
        return {
            "gun_peak": 0.28,
            "audio_peak": 0.22,
            "hook_energy": 0.15,
            "visual_dynamics": 0.20,
            "gun_sustain": 0.15,
        }
    return {
        "audio_peak": 0.25,
        "visual_dynamics": 0.25,
        "hook_energy": 0.20,
        "motion_peak": 0.15,
        "gun_peak": 0.15,
    }


def intelliclip_score(
    signals: dict[str, float],
    profile: str,
    *,
    panns_gun: float = 0.0,
    clip_score: float = 0.0,
) -> float:
    """0..1 engagement score — quso-style ranked highlights."""
    weights = profile_weights(profile)
    base = 0.0
    wsum = 0.0
    for key, w in weights.items():
        val = signals.get(key, 0.0)
        base += w * min(1.0, float(val) * (1.8 if key.startswith("gun") else 2.2))
        wsum += w
    score = base / max(wsum, 1e-6)
    if panns_gun > 0:
        score = score * 0.55 + min(1.0, panns_gun) * 0.30 + max(0.0, clip_score) * 0.15
    return round(min(1.0, max(0.0, score)), 4)


def rank_window_starts(
    video_path,
    profile: str,
    *,
    window_sec: float = 10.0,
    step_sec: float = 2.0,
    skip_intro: float = 60.0,
    min_gap: float = 75.0,
    limit: int = 48,
) -> list[tuple[float, float, str]]:
    """
    Return [(start, intelliclip_score, reason), ...] sorted by score desc.
    Uses only analyze_video bins — fast full-VOD scan like quso pre-filter.
    """
    from pathlib import Path

    analysis = load_analysis(Path(video_path))
    duration = float(analysis.get("duration") or 0.0)
    if duration < skip_intro + window_sec:
        return []

    profile = _normalize_profile(profile)
    starts: list[tuple[float, float, str]] = []
    t = skip_intro
    while t + window_sec <= duration - 30:
        sig = window_bin_signals(analysis, t, window_sec, profile)
        sc = intelliclip_score(sig, profile)
        if sc >= float(os.environ.get("INTELLICLIP_MIN_SCORE", "0.18")):
            weights = profile_weights(profile)
            top_key = max(weights, key=lambda k: sig.get(k, 0.0) * weights[k])
            reason = f"intelliclip_{top_key}={sig[top_key]:.3f}:score{sc:.3f}"
            starts.append((round(t, 1), sc, reason))
        t += step_sec

    starts.sort(key=lambda x: x[1], reverse=True)
    chosen: list[tuple[float, float, str]] = []
    for start, sc, reason in starts:
        if any(abs(start - c[0]) < min_gap for c in chosen):
            continue
        chosen.append((start, sc, reason))
        if len(chosen) >= limit:
            break
    return chosen


def merge_starts_with_anchors(
    ranked: list[tuple[float, float, str]],
    anchors: list[float],
    *,
    limit: int = 48,
) -> list[float]:
    """Owner/quso: blend AI-ranked peaks with human anchor timestamps."""
    out: dict[float, float] = {}
    for start, sc, _ in ranked:
        out[start] = sc
    for a in anchors:
        key = round(float(a) - 5.0, 1)
        if key >= 60:
            out[key] = max(out.get(key, 0.0), 0.85)
    ordered = sorted(out.items(), key=lambda x: x[1], reverse=True)
    return [s for s, _ in ordered[:limit]]


def enrich_highlight_metrics(metrics: Any, video_path, profile: str) -> None:
    """Attach intelliclip_score / hook / visual_dynamics to HighlightMetrics."""
    analysis = load_analysis(video_path)
    sig = window_bin_signals(analysis, float(metrics.start), float(metrics.duration), profile)
    ic = intelliclip_score(
        sig,
        profile,
        panns_gun=float(getattr(metrics, "panns_gun_max", 0)),
        clip_score=float(getattr(metrics, "clip_score", 0)),
    )
    metrics.hook_score = round(sig["hook_energy"], 4)
    metrics.visual_dynamics = round(sig["visual_dynamics"], 4)
    metrics.intelliclip_score = ic

    if os.environ.get("INTELLICLIP_FUSION", "1") != "1":
        return

    if metrics.rule_pass and metrics.combined_score > 0:
        metrics.combined_score = round(
            metrics.combined_score * 0.45 + ic * 0.55,
            4,
        )
    elif ic >= float(os.environ.get("INTELLICLIP_PASS_MIN", "0.42")) and metrics.audio_pass:
        metrics.rule_pass = True
        metrics.combined_score = ic
        metrics.pass_reason = f"intelliclip_ok={ic:.3f}:hook{sig['hook_energy']:.3f}"
