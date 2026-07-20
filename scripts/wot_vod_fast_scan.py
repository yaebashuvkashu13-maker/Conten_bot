#!/usr/bin/env python3
"""WoT VOD combat discovery — dense ~1 fps impact + hit-flash scan.

Ports productive practices from MLBB / Genshin / PUBG:

- Dense ~1 fps visual scan (not ≤6 sparse PANNs probes)
- Seed MERGE with stage1 peaks (HIGHLIGHT_SEED_MERGE=1) — never replace
- Soft hit-flash / center-edge thresholds (recall first; wot_brawl validates)
- Mid-VOD probe window via WOT_DENSE_PROBE_FRACTION / WINDOW_* env

Env:
  WOT_VOD_FAST_PROBE=1
  WOT_DENSE_SCAN=1                 default on
  WOT_DENSE_FPS / WOT_BOSS_SCAN_STEP_SEC  frames/sec (default 1.0)
  WOT_DENSE_MAX_SEC=900            cap long streams
  WOT_DENSE_HIT_FLASH_MIN=0.004
  WOT_DENSE_CENTER_EDGE_MIN=0.028
  WOT_DENSE_COMBINED_MIN=0.035
  WOT_DENSE_SEED_TOP_K=12
  WOT_DENSE_CLUSTER_GAP_SEC=40
  WOT_VOD_FAST_SKIP_INTRO=45
  WOT_DENSE_PROBE_FRACTION=0       mid-VOD start fraction when set
  WOT_DENSE_WINDOW_START_SEC / WOT_DENSE_WINDOW_END_SEC
"""
from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path

from highlight_scorer import WINDOW_SEC, normalize_profile

log = logging.getLogger("wot_vod_fast_scan")


def _env_float(name: str, default: float) -> float:
    raw = str(os.environ.get(name, "") or "").strip()
    if not raw:
        return float(default)
    try:
        return float(raw)
    except Exception:
        return float(default)


def _env_int(name: str, default: int) -> int:
    raw = str(os.environ.get(name, "") or "").strip()
    if not raw:
        return int(default)
    try:
        return int(float(raw))
    except Exception:
        return int(default)


def dense_scan_enabled() -> bool:
    return os.environ.get("WOT_DENSE_SCAN", "1") == "1"


def _scan_step_sec() -> float:
    # Prefer WOT_DENSE_FPS; fall back to step-sec alias used by Genshin-style configs.
    fps = _env_float("WOT_DENSE_FPS", 0.0)
    if fps > 0.1:
        return max(0.5, 1.0 / fps)
    return max(0.5, _env_float("WOT_BOSS_SCAN_STEP_SEC", 1.0))


def _skip_intro(duration: float) -> float:
    skip = _env_float("WOT_VOD_FAST_SKIP_INTRO", 45.0)
    if duration < 360:
        skip = min(skip, _env_float("WOT_VOD_FAST_SKIP_INTRO_SHORT", 20.0))
    return max(0.0, skip)


def _max_scan_sec(duration: float) -> float:
    hard = _env_float("WOT_DENSE_MAX_SEC", 900.0)
    if duration <= 480:
        return duration
    return min(duration, hard)


def _peak_gap_sec() -> float:
    return _env_float("WOT_DENSE_CLUSTER_GAP_SEC", 40.0)


def _ffmpeg_combat_frames(
    video_path: Path,
    *,
    t0: float,
    duration: float,
    step_sec: float,
) -> list[tuple[float, object]]:
    """Decode CFR frames at ~1/step fps for visual combat scoring."""
    import numpy as np

    if duration <= 0.5:
        return []
    fps = 1.0 / step_sec
    w, h = 320, 180
    frame_bytes = w * h * 3
    sample_count = max(1, int(duration / step_sec) + 1)
    max_frames = _env_int("WOT_DENSE_MAX_FRAMES", 240)
    sample_count = min(sample_count, max_frames)
    capped_dur = min(duration, sample_count * step_sec + 1.0)
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        f"{t0:.3f}",
        "-t",
        f"{capped_dur:.3f}",
        "-i",
        str(video_path),
        "-vf",
        f"fps={fps:.4f},scale={w}:{h}",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "bgr24",
        "pipe:1",
    ]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            check=False,
            timeout=max(90, int(capped_dur / 2) + 60),
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        log.warning("wot dense ffmpeg failed: %s", exc)
        return []
    if proc.returncode != 0 or not proc.stdout:
        return []

    raw = proc.stdout
    frames: list[tuple[float, object]] = []
    for idx in range(sample_count):
        offset = idx * frame_bytes
        chunk = raw[offset : offset + frame_bytes]
        if len(chunk) < frame_bytes:
            break
        frame = np.frombuffer(chunk, dtype=np.uint8).reshape((h, w, 3)).copy()
        t = float(t0) + float(idx) * step_sec
        frames.append((t, frame))
        if len(frames) >= max_frames:
            break
    return frames


def _frame_combat_scores(frame) -> tuple[float, float, float]:
    """Return (hit_flash, center_edge, combined) via visual_action_check."""
    from visual_action_check import check_frame_visual

    _ok, _reason, metrics = check_frame_visual("wot", frame)
    hit = float(metrics.get("hit_flash") or 0.0)
    edge = float(metrics.get("center_edge") or 0.0)
    combined = edge + 1.4 * hit
    return hit, edge, combined


def _cluster_seed_times(scored: list[tuple[float, float]], *, gap_sec: float, top_k: int) -> list[float]:
    """scored = [(combined, t), ...] strongest first → chronological cluster reps."""
    if not scored:
        return []
    # Greedy: take strongest, suppress nearby, repeat.
    picks: list[float] = []
    for _score, t in scored:
        if any(abs(t - p) < gap_sec for p in picks):
            continue
        picks.append(float(t))
        if len(picks) >= top_k:
            break
    return sorted(picks)


def dense_combat_peaks(video_path: Path) -> tuple[list[float], str]:
    """Dense scan → seed peak times + reason string."""
    from smart_video_editor import ffprobe_duration

    dur = float(ffprobe_duration(video_path) or 0.0)
    if dur <= 5.0:
        return [], "fast_probe_no_duration" if dur <= 0 else "fast_probe_too_short"

    skip = _skip_intro(dur)
    win_start_env = str(os.environ.get("WOT_DENSE_WINDOW_START_SEC", "") or "").strip()
    win_end_env = str(os.environ.get("WOT_DENSE_WINDOW_END_SEC", "") or "").strip()
    if win_start_env and win_end_env:
        try:
            t0 = max(0.0, float(win_start_env))
            t1 = min(dur, float(win_end_env))
        except Exception:
            t0 = skip
            t1 = _max_scan_sec(dur)
    else:
        frac = _env_float("WOT_DENSE_PROBE_FRACTION", 0.0)
        if frac > 0.05:
            t0 = max(skip, dur * min(0.85, frac))
        else:
            t0 = skip
        t1 = min(dur, t0 + _max_scan_sec(dur - t0))
    if t1 - t0 < 20.0:
        return [], f"fast_probe_too_short=dur{dur:.0f}"

    step = _scan_step_sec()
    frames = _ffmpeg_combat_frames(video_path, t0=t0, duration=t1 - t0, step_sec=step)
    if not frames:
        return [], "wot_dense_no_frames"

    flash_min = _env_float("WOT_DENSE_HIT_FLASH_MIN", 0.004)
    edge_min = _env_float("WOT_DENSE_CENTER_EDGE_MIN", 0.028)
    combined_min = _env_float("WOT_DENSE_COMBINED_MIN", 0.035)
    top_k = _env_int("WOT_DENSE_SEED_TOP_K", 12)
    gap = _peak_gap_sec()

    hits_scored: list[tuple[float, float]] = []
    best_flash = 0.0
    best_edge = 0.0
    hit_n = 0
    for t, frame in frames:
        flash, edge, combined = _frame_combat_scores(frame)
        best_flash = max(best_flash, flash)
        best_edge = max(best_edge, edge)
        if flash >= flash_min or edge >= edge_min or combined >= combined_min:
            hit_n += 1
            hits_scored.append((combined, float(t)))

    hits_scored.sort(key=lambda row: row[0], reverse=True)
    seeds = _cluster_seed_times(hits_scored, gap_sec=gap, top_k=top_k)
    reason = (
        f"fast_wot_dense_{len(seeds)}/{len(frames)} "
        f"hits={hit_n} top_flash={best_flash:.3f} top_edge={best_edge:.3f}"
    )
    log.info(
        "wot dense: frames=%d hits=%d seeds=%d window=%.0f-%.0f %s",
        len(frames),
        hit_n,
        len(seeds),
        t0,
        t1,
        reason,
    )
    if not seeds:
        return [], f"fast_wot_0/{len(frames)} top_flash={best_flash:.3f} top_edge={best_edge:.3f}"
    return seeds, reason


# --- Legacy sparse PANNs path -------------------------------------------------


def _probe_offsets(duration: float, *, skip_intro: float) -> list[float]:
    dur = max(0.0, float(duration))
    if dur < skip_intro + 90:
        return []
    offsets: list[float] = []
    for delta in (0, 200, 480, 900, 1400, 2000):
        t = skip_intro + delta
        if t + WINDOW_SEC < dur - 45:
            offsets.append(round(t, 1))
    return sorted(set(offsets))[: _env_int("WOT_VOD_FAST_PROBE_MAX", 6)]


def _legacy_sparse_check(video_path: Path) -> tuple[bool, str, list[float]]:
    from highlight_scorer import score_panns_audio
    from smart_video_editor import ffprobe_duration

    dur = ffprobe_duration(video_path)
    if dur <= 0:
        return False, "fast_probe_no_duration", []
    skip = _env_float("WOT_VOD_FAST_SKIP_INTRO", 90.0)
    offsets = _probe_offsets(dur, skip_intro=skip)
    if not offsets:
        return False, "fast_probe_too_short", []
    impact_min = _env_float("WOT_VOD_FAST_IMPACT_MIN", 0.10)
    hits: list[float] = []
    top_impact = 0.0
    for t in offsets:
        panns = score_panns_audio(video_path, t, WINDOW_SEC)
        impact = float(panns.get("panns_impact_max", 0) or panns.get("panns_gun_max", 0))
        top_impact = max(top_impact, impact)
        if impact >= impact_min:
            hits.append(t)
    if not hits:
        return False, f"fast_wot_0/{len(offsets)} top_impact={top_impact:.3f}", []
    return True, f"fast_wot_{len(hits)}/{len(offsets)} top_impact={top_impact:.3f}", hits


def vod_fast_impact_check(
    video_path: Path,
    profile: str = "wot",
) -> tuple[bool, str, list[float]]:
    """Public fast-probe entry used by shooter_vod_segment_feed."""
    _ = normalize_profile(profile)
    if os.environ.get("WOT_VOD_FAST_PROBE", "1") != "1":
        return True, "fast_probe_disabled", []

    if dense_scan_enabled():
        peaks, reason = dense_combat_peaks(video_path)
        if not peaks:
            return False, reason, []
        return True, reason, peaks

    return _legacy_sparse_check(video_path)


def apply_fast_probe_seeds(peaks: list[float]) -> None:
    if not peaks or os.environ.get("WOT_VOD_SEED_FROM_FAST_PROBE", "1") != "1":
        return
    os.environ["HIGHLIGHT_ALLOW_SEED_STARTS"] = "1"
    # Merge seeds into stage1 (same as Genshin dense path) — never replace.
    os.environ["HIGHLIGHT_SEED_MERGE"] = os.environ.get("HIGHLIGHT_SEED_MERGE", "1")
    os.environ["HIGHLIGHT_SEED_STARTS"] = ",".join(str(round(p, 1)) for p in peaks[:24])


def clear_fast_probe_seeds() -> None:
    os.environ.pop("HIGHLIGHT_SEED_STARTS", None)
    os.environ.pop("HIGHLIGHT_ALLOW_SEED_STARTS", None)
    os.environ.pop("HIGHLIGHT_SEED_MERGE", None)


__all__ = [
    "apply_fast_probe_seeds",
    "clear_fast_probe_seeds",
    "dense_combat_peaks",
    "dense_scan_enabled",
    "vod_fast_impact_check",
]
