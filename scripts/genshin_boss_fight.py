#!/usr/bin/env python3
"""Expand Genshin highlight peaks to the full boss fight window.

Boss fights often last 30–90s. Highlight discovery scores a short window (~10s);
this module walks combat/boss-bar sustain around the peak so the sent clip covers
the whole fight, not a mid-fight fragment.
"""

from __future__ import annotations

import os
from pathlib import Path

import numpy as np

_CACHE: dict[str, dict] = {}


def _min_sec() -> float:
    return float(os.environ.get("GENSHIN_BOSS_FIGHT_MIN_SEC", "28"))


def _max_sec() -> float:
    return float(os.environ.get("GENSHIN_BOSS_FIGHT_MAX_SEC", "90"))


def _hard_max_sec() -> float:
    return float(os.environ.get("GENSHIN_BOSS_FIGHT_HARD_MAX_SEC", "120"))


def _lead_sec() -> float:
    return float(os.environ.get("GENSHIN_VOD_LEAD_SEC", os.environ.get("MLBB_VOD_LEAD_SEC", "3")))


def _bar_step_sec() -> float:
    return float(os.environ.get("GENSHIN_BOSS_FIGHT_BAR_STEP_SEC", "4"))


def _bar_keep() -> float:
    return float(os.environ.get("GENSHIN_BOSS_FIGHT_BAR_KEEP", "0.12"))


def _quiet_bins() -> int:
    return max(1, int(os.environ.get("GENSHIN_BOSS_FIGHT_QUIET_BINS", "3")))


def variable_length_enabled() -> bool:
    return os.environ.get(
        "GENSHIN_BOSS_FULL_FIGHT",
        os.environ.get("SHOOTER_VOD_VARIABLE_LENGTH", "1"),
    ) == "1"


def _analysis_for(vod: Path) -> dict:
    from vod_analysis_cache import analyze_video_cached, cache_key_hash

    key = cache_key_hash(vod)
    if key not in _CACHE:
        _CACHE[key] = analyze_video_cached(vod)
    return _CACHE[key]


def clear_analysis_cache() -> None:
    _CACHE.clear()


def _sample_boss_bar(vod: Path, t: float, *, crop_box=None) -> float:
    from gameplay_gate import _genshin_boss_bar_score, _read_frame_at

    frame = _read_frame_at(vod, float(t))
    if frame is None:
        return 0.0
    if crop_box is not None:
        x, y, w, h = crop_box
        frame = frame[y : y + h, x : x + w]
    return float(_genshin_boss_bar_score(frame))


def _boss_bar_series(
    vod: Path,
    t0: float,
    t1: float,
    *,
    step: float,
) -> list[tuple[float, float]]:
    """Batch-decode boss-bar scores over [t0,t1] at ~1/step fps (fast path)."""
    import subprocess

    from gameplay_gate import _genshin_boss_bar_score

    span = max(0.0, t1 - t0)
    if span < 1.0:
        return [(t0, _sample_boss_bar(vod, t0))]
    fps = 1.0 / max(1.0, step)
    w, h = 320, 180
    frame_bytes = w * h * 3
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        f"{t0:.3f}",
        "-t",
        f"{span:.3f}",
        "-i",
        str(vod),
        "-vf",
        f"fps={fps:.4f},scale={w}:{h}",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "bgr24",
        "pipe:1",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, check=False, timeout=max(60, int(span) + 30))
    except (subprocess.TimeoutExpired, OSError):
        proc = None
    out: list[tuple[float, float]] = []
    if proc is not None and proc.returncode == 0 and proc.stdout:
        raw = proc.stdout
        idx = 0
        while True:
            offset = idx * frame_bytes
            chunk = raw[offset : offset + frame_bytes]
            if len(chunk) < frame_bytes:
                break
            frame = np.frombuffer(chunk, dtype=np.uint8).reshape((h, w, 3)).copy()
            out.append((t0 + idx * step, float(_genshin_boss_bar_score(frame))))
            idx += 1
    if out:
        return out
    # Fallback: sparse seeks
    t = t0
    while t <= t1:
        out.append((t, _sample_boss_bar(vod, t)))
        t += step
    return out


def _expand_by_boss_bar(
    vod: Path,
    start: float,
    end: float,
    file_dur: float,
    *,
    crop_box=None,
) -> tuple[float, float]:
    """Walk outward while boss HP bar stays present — one ffmpeg batch, not N seeks."""
    step = max(2.0, _bar_step_sec())
    keep = _bar_keep()
    hard = _hard_max_sec()
    # Probe window around current bounds (enough to grow to hard max)
    pad = hard
    t0 = max(0.0, start - pad)
    t1 = min(file_dur, end + pad)
    series = _boss_bar_series(vod, t0, t1, step=step)
    if not series:
        return start, end

    # Map times near start/end
    left = start
    right = end
    # extend left: walk series backward from start
    miss = 0
    for t, bar in reversed([(t, b) for t, b in series if t <= start + 0.01]):
        if (right - t) > hard:
            break
        if bar >= keep:
            left = t
            miss = 0
        else:
            miss += 1
            if miss >= 2 and t < start:
                break
    # extend right
    miss = 0
    for t, bar in [(t, b) for t, b in series if t >= end - 0.01]:
        if (t - left) > hard:
            break
        if bar >= keep:
            right = min(file_dur, t + step * 0.5)
            miss = 0
        else:
            miss += 1
            if miss >= 2 and t > end:
                break
    return left, right


def detect_boss_fight_bounds(vod: Path, peak_sec: float) -> tuple[float, float, float]:
    """
    Detect full boss-fight window around peak_sec.

    Returns (start_sec, end_sec, duration_sec).
    """
    min_d = _min_sec()
    max_d = _max_sec()
    hard_max = _hard_max_sec()
    lead = _lead_sec()
    analysis = _analysis_for(vod)
    win = float(analysis.get("window_seconds", 2.0))
    file_dur = float(analysis.get("duration", 0.0))
    bins = int(analysis.get("bins", 0))
    if bins < 2 or file_dur <= 0:
        start = max(0.0, float(peak_sec) - lead)
        end = min(file_dur if file_dur > 0 else start + min_d, start + min(max_d, 45.0))
        return round(start, 2), round(end, 2), round(max(0.0, end - start), 2)

    motion = np.asarray(analysis["center_motion"], dtype=np.float32)
    audio = np.asarray(analysis["audio"], dtype=np.float32)
    scene = np.asarray(analysis["scene"], dtype=np.float32)
    combined = motion * 0.40 + audio * 0.35 + scene * 0.25

    sustain_thr = float(np.percentile(combined, 38)) if bins > 4 else float(combined.max()) * 0.70
    motion_thr = float(np.percentile(motion, 48)) if bins > 3 else float(motion.max()) * 0.45

    peak_idx = int(round(float(peak_sec) / win))
    peak_idx = max(0, min(bins - 1, peak_idx))
    extend = int(hard_max / max(win, 0.5)) + 4
    quiet_need = _quiet_bins()

    left = peak_idx
    quiet = 0
    while left > 0 and peak_idx - left < extend:
        probe = left - 1
        active = combined[probe] >= sustain_thr or motion[probe] >= motion_thr
        left = probe
        if active:
            quiet = 0
        else:
            quiet += 1
            if quiet >= quiet_need:
                break

    right = peak_idx
    quiet = 0
    while right < bins - 1 and right - peak_idx < extend:
        probe = right + 1
        active = combined[probe] >= sustain_thr * 0.90 or motion[probe] >= motion_thr * 0.92
        right = probe
        if active:
            quiet = 0
        else:
            quiet += 1
            if quiet >= quiet_need:
                break

    region_start = left * win
    region_end = min(file_dur, (right + 1) * win)

    # Prefer covering the peak with a short lead-in.
    start = max(0.0, min(region_start, float(peak_sec) - lead))
    end = min(file_dur, max(region_end, float(peak_sec) + lead))

    if os.environ.get("GENSHIN_BOSS_FIGHT_BAR_EXPAND", "1") == "1":
        try:
            from gameplay_gate import detect_game_viewport_crop

            crop = detect_game_viewport_crop(vod, float(peak_sec), min(12.0, max_d))
            start, end = _expand_by_boss_bar(vod, start, end, file_dur, crop_box=crop)
        except Exception:
            pass

    dur = end - start
    if dur < min_d:
        # Grow symmetrically around peak when sustain window is too short.
        half = min_d / 2.0
        start = max(0.0, float(peak_sec) - half)
        end = min(file_dur, start + min_d)
        if end - start < min_d:
            start = max(0.0, end - min_d)
        dur = end - start

    if dur > hard_max:
        # Keep peak inside the hard cap window.
        start = max(0.0, min(start, float(peak_sec) - lead))
        end = min(file_dur, start + hard_max)
        if float(peak_sec) > end:
            end = min(file_dur, float(peak_sec) + lead)
            start = max(0.0, end - hard_max)
        dur = end - start
    elif dur > max_d and os.environ.get("GENSHIN_BOSS_FIGHT_TRIM_LONG", "0") == "1":
        end = min(file_dur, max(region_end, float(peak_sec) + lead))
        start = max(0.0, end - max_d)
        dur = end - start

    return round(start, 2), round(end, 2), round(dur, 2)


def expand_clip_to_full_boss_fight(vod: Path, clip: dict) -> dict:
    """Rewrite clip start/duration to cover the full boss fight around peak."""
    if not variable_length_enabled():
        return clip
    peak = float(clip.get("peak_start", clip.get("start", 0)))
    start, end, dur = detect_boss_fight_bounds(vod, peak)
    if dur <= 0:
        return clip
    return {
        **clip,
        "start": start,
        "peak_start": peak,
        "fight_end": end,
        "input_duration": dur,
        "output_duration": dur,
        "boss_fight_full": True,
        "boss_fight_dur": dur,
    }
