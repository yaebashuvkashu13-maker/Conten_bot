#!/usr/bin/env python3
"""Genshin VOD boss discovery — dense 1 fps boss HP bar scan (MLBB-style).

Instead of 6 sparse probes (misses short boss VODs), decode ~1 frame/sec and
score the boss HP bar. Returns seed peak starts for highlight stage1.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from highlight_scorer import normalize_profile


def _scan_step_sec() -> float:
    """1.0 = one frame per second (same idea as MLBB banner scan density)."""
    return max(0.5, float(os.environ.get("GENSHIN_BOSS_SCAN_STEP_SEC", "1.0")))


def _bar_min() -> float:
    return float(os.environ.get("GENSHIN_VOD_FAST_BAR_MIN", "0.12"))


def _skip_intro(duration: float) -> float:
    skip = float(os.environ.get("GENSHIN_VOD_FAST_SKIP_INTRO", "45"))
    if duration < 360:
        skip = min(skip, float(os.environ.get("GENSHIN_VOD_FAST_SKIP_INTRO_SHORT", "15")))
    return max(0.0, skip)


def _max_scan_sec(duration: float) -> float:
    """Cap long streams so 1fps stays fast; short VODs scan fully."""
    hard = float(os.environ.get("GENSHIN_BOSS_SCAN_MAX_SEC", "900"))
    if duration <= 480:
        return duration
    return min(duration, hard)


def _peak_gap_sec() -> float:
    return float(os.environ.get("GENSHIN_BOSS_SCAN_PEAK_GAP_SEC", "25"))


def _ffmpeg_boss_frames(
    video_path: Path,
    *,
    t0: float,
    duration: float,
    step_sec: float,
) -> list[tuple[float, object]]:
    """Decode CFR frames at ~1/step fps, scaled small for HSV boss-bar score."""
    import numpy as np

    if duration <= 0.5:
        return []
    fps = 1.0 / step_sec
    # 320x180 keeps boss-bar HSV cheap (same scale as gameplay_gate helpers)
    w, h = 320, 180
    frame_bytes = w * h * 3
    sample_count = max(1, int(duration / step_sec) + 1)
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        f"{t0:.3f}",
        "-t",
        f"{duration:.3f}",
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
            timeout=max(90, int(duration / 2) + 60),
        )
    except (subprocess.TimeoutExpired, OSError):
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
        sec = t0 + idx * step_sec
        frames.append((sec, frame))
    return frames


def _fallback_seek_frames(
    video_path: Path,
    *,
    t0: float,
    t1: float,
    step_sec: float,
) -> list[tuple[float, object]]:
    from gameplay_gate import _read_frame_at

    out: list[tuple[float, object]] = []
    t = t0
    while t <= t1:
        frame = _read_frame_at(video_path, t)
        if frame is not None:
            out.append((t, frame))
        t += step_sec
    return out


def dense_boss_bar_peaks(
    video_path: Path,
    *,
    step_sec: float | None = None,
) -> tuple[list[float], str]:
    """
    Scan VOD at ~1 fps for boss HP bar. Returns (peak_starts, reason).
    Peaks are clustered with gap so we don't flood stage1.
    """
    from gameplay_gate import _genshin_boss_bar_score
    from smart_video_editor import ffprobe_duration

    dur = ffprobe_duration(video_path)
    if dur <= 0:
        return [], "dense_no_duration"

    step = step_sec if step_sec is not None else _scan_step_sec()
    skip = _skip_intro(dur)
    scan_end = _max_scan_sec(dur)
    if scan_end - skip < 20:
        # Tiny clip: probe middle once
        mid = max(3.0, dur * 0.4)
        frames = _fallback_seek_frames(video_path, t0=mid, t1=mid, step_sec=1.0)
        if not frames:
            return [], f"dense_too_short=dur{dur:.0f}"
        bar = float(_genshin_boss_bar_score(frames[0][1]))
        if bar >= _bar_min():
            return [round(mid, 1)], f"dense_mid_hit bar={bar:.3f}"
        return [], f"dense_mid_miss bar={bar:.3f}"

    frames = _ffmpeg_boss_frames(
        video_path, t0=skip, duration=scan_end - skip, step_sec=step
    )
    if not frames:
        frames = _fallback_seek_frames(
            video_path, t0=skip, t1=scan_end - 1.0, step_sec=max(step, 2.0)
        )
    if not frames:
        return [], "dense_no_frames"

    bar_min = _bar_min()
    scored: list[tuple[float, float]] = []
    top_bar = 0.0
    for sec, frame in frames:
        bar = float(_genshin_boss_bar_score(frame))
        top_bar = max(top_bar, bar)
        if bar >= bar_min:
            scored.append((bar, sec))

    if not scored:
        return [], f"dense_0/{len(frames)} top_bar={top_bar:.3f} step={step}"

    scored.sort(key=lambda row: row[0], reverse=True)
    gap = _peak_gap_sec()
    peaks: list[float] = []
    for bar, sec in scored:
        if any(abs(sec - p) < gap for p in peaks):
            continue
        peaks.append(round(sec, 1))
        if len(peaks) >= int(os.environ.get("GENSHIN_BOSS_SCAN_MAX_PEAKS", "24")):
            break
    peaks.sort()
    return peaks, f"dense_{len(peaks)}/{len(frames)} top_bar={top_bar:.3f} step={step}"


def vod_fast_boss_check(
    video_path: Path,
    profile: str = "genshin",
) -> tuple[bool, str, list[float]]:
    """Preflight used by shooter feed — dense 1fps boss scan."""
    profile = normalize_profile(profile)
    if os.environ.get("GENSHIN_VOD_FAST_PROBE", "1") != "1":
        return True, "fast_probe_disabled", []

    # Legacy sparse path only if explicitly requested
    if os.environ.get("GENSHIN_VOD_DENSE_SCAN", "1") != "1":
        return _legacy_sparse_check(video_path)

    peaks, reason = dense_boss_bar_peaks(video_path)
    if not peaks:
        return False, reason, []
    return True, reason, peaks


def _legacy_sparse_check(video_path: Path) -> tuple[bool, str, list[float]]:
    from gameplay_gate import _genshin_boss_bar_score, _read_frame_at
    from smart_video_editor import ffprobe_duration

    dur = ffprobe_duration(video_path)
    if dur <= 0:
        return False, "fast_probe_no_duration", []
    skip = _skip_intro(dur)
    offsets = []
    for delta in (0, 30, 60, 90, 120, 180, 240):
        t = skip + delta
        if t < dur - 15:
            offsets.append(round(t, 1))
    if not offsets:
        return False, f"fast_probe_too_short=dur{dur:.0f}", []
    bar_min = _bar_min()
    hits: list[float] = []
    top = 0.0
    for t in offsets:
        frame = _read_frame_at(video_path, t)
        if frame is None:
            continue
        bar = float(_genshin_boss_bar_score(frame))
        top = max(top, bar)
        if bar >= bar_min:
            hits.append(t)
    if not hits:
        return False, f"fast_genshin_0/{len(offsets)} top_bar={top:.3f}", []
    return True, f"fast_genshin_{len(hits)}/{len(offsets)} top_bar={top:.3f}", hits


def apply_fast_probe_seeds(peaks: list[float]) -> None:
    if not peaks or os.environ.get("GENSHIN_VOD_SEED_FROM_FAST_PROBE", "1") != "1":
        return
    os.environ["HIGHLIGHT_ALLOW_SEED_STARTS"] = "1"
    # Genshin: merge seeds into stage1 (don't replace full analysis)
    os.environ["HIGHLIGHT_SEED_MERGE"] = os.environ.get("HIGHLIGHT_SEED_MERGE", "1")
    os.environ["HIGHLIGHT_SEED_STARTS"] = ",".join(str(round(p, 1)) for p in peaks[:24])


def clear_fast_probe_seeds() -> None:
    os.environ.pop("HIGHLIGHT_SEED_STARTS", None)
    os.environ.pop("HIGHLIGHT_ALLOW_SEED_STARTS", None)
    os.environ.pop("HIGHLIGHT_SEED_MERGE", None)
