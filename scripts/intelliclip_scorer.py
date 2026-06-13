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
from pathlib import Path
from typing import Any

import numpy as np

log = logging.getLogger("intelliclip")

HOOK_SEC = float(os.environ.get("INTELLICLIP_HOOK_SEC", "3.0"))
CACHE_DIR = Path(os.environ.get("INTELLICLIP_CACHE_DIR", "/root/data/mlbb/analysis_cache"))
MAX_CLIPS_QUSO = int(os.environ.get("INTELLICLIP_MAX_CLIPS", "5"))
MAX_COVERAGE_RATIO = float(os.environ.get("INTELLICLIP_MAX_COVERAGE", "0.30"))


def _normalize_profile(profile: str) -> str:
    p = profile.strip().lower()
    if p == "mlbb":
        return "mobile_legends"
    if p == "world_of_tanks":
        return "wot"
    return p


def _analysis_cache_path(video_path: Path) -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return CACHE_DIR / f"{video_path.stem}.npz"


def _save_analysis_cache(video_path: Path, analysis: dict[str, Any]) -> None:
    cache = _analysis_cache_path(video_path)
    payload: dict[str, Any] = {"duration": analysis.get("duration"), "window_seconds": analysis.get("window_seconds")}
    for key in ("audio", "gunfire", "center_motion", "scene", "motion", "sharpness"):
        val = analysis.get(key)
        if val is not None:
            payload[key] = np.asarray(val, dtype=np.float32)
    np.savez_compressed(cache, **payload)


def _load_analysis_cache(video_path: Path) -> dict[str, Any] | None:
    cache = _analysis_cache_path(video_path)
    if not cache.exists():
        return None
    try:
        if cache.stat().st_mtime < video_path.stat().st_mtime:
            return None
        with np.load(cache, allow_pickle=False) as data:
            out: dict[str, Any] = {
                "duration": float(data["duration"]),
                "window_seconds": float(data["window_seconds"]),
            }
            for key in data.files:
                if key in ("duration", "window_seconds"):
                    continue
                out[key] = np.asarray(data[key], dtype=np.float32)
            if "gunfire" not in out and "audio" in out:
                out["gunfire"] = out["audio"]
            return out
    except (OSError, KeyError, ValueError) as exc:
        log.warning("analysis cache read failed %s: %s", cache, exc)
        return None


@lru_cache(maxsize=8)
def _load_analysis_mem(video_key: str, video_path: str, mtime_ns: int) -> dict[str, Any]:
    del mtime_ns
    from smart_video_editor import analyze_video

    p = Path(video_path)
    cached = _load_analysis_cache(p)
    if cached is not None:
        return cached
    analysis = analyze_video(p)
    try:
        _save_analysis_cache(p, analysis)
    except OSError as exc:
        log.warning("analysis cache write failed: %s", exc)
    return analysis


def load_analysis(video_path) -> dict[str, Any]:
    p = Path(video_path).resolve()
    return _load_analysis_mem(str(p), str(p), p.stat().st_mtime_ns)


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


def _rank_windows_in_range(
    analysis: dict[str, Any],
    profile: str,
    *,
    t_start: float,
    t_end: float,
    window_sec: float,
    step_sec: float,
    min_score: float,
) -> list[tuple[float, float, str]]:
    profile = _normalize_profile(profile)
    weights = profile_weights(profile)
    out: list[tuple[float, float, str]] = []
    t = t_start
    while t + window_sec <= t_end:
        sig = window_bin_signals(analysis, t, window_sec, profile)
        sc = intelliclip_score(sig, profile)
        if sc >= min_score:
            top_key = max(weights, key=lambda k: sig.get(k, 0.0) * weights[k])
            reason = f"intelliclip_{top_key}={sig[top_key]:.3f}:score{sc:.3f}"
            out.append((round(t, 1), sc, reason))
        t += step_sec
    return out


def _dedupe_ranked(
    starts: list[tuple[float, float, str]],
    *,
    min_gap: float,
    limit: int,
) -> list[tuple[float, float, str]]:
    starts.sort(key=lambda x: x[1], reverse=True)
    chosen: list[tuple[float, float, str]] = []
    for start, sc, reason in starts:
        if any(abs(start - c[0]) < min_gap for c in chosen):
            continue
        chosen.append((start, sc, reason))
        if len(chosen) >= limit:
            break
    return chosen


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

    min_score = float(os.environ.get("INTELLICLIP_MIN_SCORE", "0.18"))
    # Long VODs: coarser global scan (quso pre-filter), fine pass near anchors later.
    eff_step = step_sec
    if duration > 3600:
        eff_step = max(step_sec, float(os.environ.get("INTELLICLIP_LONG_STEP_SEC", "6")))
    starts = _rank_windows_in_range(
        analysis,
        profile,
        t_start=skip_intro,
        t_end=duration - 30,
        window_sec=window_sec,
        step_sec=eff_step,
        min_score=min_score,
    )
    return _dedupe_ranked(starts, min_gap=min_gap, limit=limit)


def rank_near_anchors(
    video_path,
    profile: str,
    anchors: list[float],
    *,
    window_sec: float = 10.0,
    span_sec: float = 180.0,
    step_sec: float = 4.0,
    min_gap: float = 45.0,
    limit: int = 36,
) -> list[tuple[float, float, str]]:
    """Fine intelliclip scan around owner anchors (quso custom timestamps)."""
    if not anchors:
        return []
    analysis = load_analysis(Path(video_path))
    duration = float(analysis.get("duration") or 0.0)
    min_score = float(os.environ.get("INTELLICLIP_ANCHOR_MIN_SCORE", "0.14"))
    merged: list[tuple[float, float, str]] = []
    for anchor in anchors:
        lo = max(60.0, float(anchor) - span_sec)
        hi = min(duration - 30, float(anchor) + span_sec)
        merged.extend(
            _rank_windows_in_range(
                analysis,
                profile,
                t_start=lo,
                t_end=hi,
                window_sec=window_sec,
                step_sec=step_sec,
                min_score=min_score,
            )
        )
    return _dedupe_ranked(merged, min_gap=min_gap, limit=limit)


def rank_hybrid_starts(
    video_path,
    profile: str,
    anchors: list[float] | None = None,
    *,
    window_sec: float = 10.0,
    limit: int = 48,
) -> list[tuple[float, float, str]]:
    """CutMagic-like peaks + anchor neighborhoods — quso two-layer candidate pool."""
    global_ranked = rank_window_starts(video_path, profile, window_sec=window_sec, limit=limit)
    if not anchors:
        return global_ranked
    local_ranked = rank_near_anchors(
        video_path,
        profile,
        anchors,
        window_sec=window_sec,
        limit=max(24, limit // 2),
    )
    pool: dict[float, tuple[float, float, str]] = {}
    for row in global_ranked + local_ranked:
        pool[row[0]] = row if row[0] not in pool or row[1] > pool[row[0]][1] else pool[row[0]]
    ordered = sorted(pool.values(), key=lambda x: x[1], reverse=True)
    return ordered[:limit]


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
    boost = float(os.environ.get("INTELLICLIP_ANCHOR_BOOST", "0.88"))
    for a in anchors:
        key = round(float(a) - 5.0, 1)
        if key >= 60:
            out[key] = max(out.get(key, 0.0), boost)
    ordered = sorted(out.items(), key=lambda x: x[1], reverse=True)
    return [s for s, _ in ordered[:limit]]


def candidate_intelliclip_score(cand: dict, anchors: list[float] | None = None) -> float:
    hm = cand.get("highlight_metrics") or cand.get("strict_metrics") or {}
    base = float(
        hm.get("intelliclip_score")
        or hm.get("combined_score")
        or cand.get("score", 0.0)
    )
    if not anchors:
        return base
    start = float(cand.get("start", 0))
    near = min(abs(start + 5.0 - float(a)) for a in anchors)
    bonus = float(os.environ.get("INTELLICLIP_ANCHOR_NEAR_BONUS", "0.22"))
    window = float(os.environ.get("INTELLICLIP_ANCHOR_NEAR_SEC", "90"))
    if near <= window:
        base += bonus * (1.0 - near / max(window, 1.0))
    return base


def select_intelliclip_clips(
    candidates: list[dict],
    *,
    video_duration: float | None = None,
    max_clips: int = MAX_CLIPS_QUSO,
    max_coverage: float = MAX_COVERAGE_RATIO,
    min_gap: float = 75.0,
    anchors: list[float] | None = None,
) -> list[dict]:
    """
    Quso Intelliclips final pick: top 3–5 moments, <=30% of source length, spaced apart.
    Prefers strong hook_energy in the first seconds of each window.
    """
    if not candidates:
        return []
    ranked = sorted(
        candidates,
        key=lambda c: (
            candidate_intelliclip_score(c, anchors),
            float((c.get("highlight_metrics") or {}).get("hook_score", 0)),
            float((c.get("highlight_metrics") or {}).get("panns_gun_max", 0)),
        ),
        reverse=True,
    )
    cap_dur = (video_duration or 0) * max_coverage if video_duration else 0
    chosen: list[dict] = []
    used = 0.0
    for cand in ranked:
        start = float(cand.get("start", 0))
        if any(abs(start - float(c.get("start", 0))) < min_gap for c in chosen):
            continue
        seg_dur = float(cand.get("output_duration") or cand.get("input_duration") or 10.0)
        if cap_dur > 0 and used + seg_dur > cap_dur and len(chosen) >= 3:
            continue
        chosen.append(cand)
        used += seg_dur
        if len(chosen) >= max_clips:
            break

    if anchors:
        min_anchor_hits = int(os.environ.get("INTELLICLIP_MIN_ANCHOR_HITS", "2"))
        near_sec = float(os.environ.get("INTELLICLIP_ANCHOR_NEAR_SEC", "90"))
        anchor_pool = [
            c
            for c in candidates
            if min(abs(float(c.get("start", 0)) + 5.0 - float(a)) for a in anchors) <= near_sec
        ]
        anchor_pool.sort(key=lambda c: candidate_intelliclip_score(c, anchors), reverse=True)
        hits = sum(
            1
            for c in chosen
            if min(abs(float(c.get("start", 0)) + 5.0 - float(a)) for a in anchors) <= near_sec
        )
        for extra in anchor_pool:
            if hits >= min_anchor_hits:
                break
            if extra in chosen:
                continue
            start = float(extra.get("start", 0))
            if any(abs(start - float(c.get("start", 0))) < min_gap for c in chosen):
                continue
            if len(chosen) >= max_clips and chosen:
                chosen[-1] = extra
            else:
                chosen.append(extra)
            hits += 1
    return chosen


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
