#!/usr/bin/env python3
"""Expand Genshin highlight peaks to the full boss fight window.

Boss fights often last 30–90s. Discovery scores a short peak (~10s); this module
finds the contiguous boss-HP-bar run that contains the peak and cuts from the
fight START (not mid-fight).
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
    """Seconds before boss bar appears (approach / lock-on)."""
    return float(os.environ.get("GENSHIN_VOD_LEAD_SEC", "5"))


def _bar_step_sec() -> float:
    return float(os.environ.get("GENSHIN_BOSS_FIGHT_BAR_STEP_SEC", "2"))


def _bar_keep() -> float:
    return float(os.environ.get("GENSHIN_BOSS_FIGHT_BAR_KEEP", "0.12"))


def _gap_tolerate() -> int:
    """Allow this many low-bar samples inside a fight without splitting the run."""
    return max(0, int(os.environ.get("GENSHIN_BOSS_FIGHT_GAP_TOLERATE", "3")))


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
    """Batch-decode boss-bar scores over [t0,t1] at ~1/step fps."""
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
        proc = subprocess.run(
            cmd, capture_output=True, check=False, timeout=max(60, int(span) + 30)
        )
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
    t = t0
    while t <= t1:
        out.append((t, _sample_boss_bar(vod, t)))
        t += step
    return out


def _bar_runs(
    series: list[tuple[float, float]],
    *,
    keep: float,
    gap_tolerate: int,
) -> list[tuple[float, float, float]]:
    """
    Contiguous boss-bar runs allowing short gaps (camera cuts / flicker).
    Returns list of (run_start, run_end, max_bar).
    """
    if not series:
        return []
    runs: list[tuple[float, float, float]] = []
    run_start: float | None = None
    run_end: float | None = None
    run_max = 0.0
    miss = 0
    for t, bar in series:
        if bar >= keep:
            if run_start is None:
                run_start = t
            run_end = t
            run_max = max(run_max, bar)
            miss = 0
        else:
            if run_start is None:
                continue
            miss += 1
            if miss > gap_tolerate:
                runs.append((run_start, run_end or run_start, run_max))
                run_start = None
                run_end = None
                run_max = 0.0
                miss = 0
    if run_start is not None:
        runs.append((run_start, run_end or run_start, run_max))
    return runs


def _run_containing_peak(
    runs: list[tuple[float, float, float]],
    peak: float,
    *,
    pad: float = 8.0,
) -> tuple[float, float, float] | None:
    for start, end, mx in runs:
        if start - pad <= peak <= end + pad:
            return start, end, mx
    # nearest run by center distance
    if not runs:
        return None
    return min(runs, key=lambda r: abs((r[0] + r[1]) / 2.0 - peak))


def detect_boss_fight_bounds(vod: Path, peak_sec: float) -> tuple[float, float, float]:
    """
    Detect full boss-fight window around peak_sec.

    Prefer the START of the boss-bar run that contains the peak.
    If the run is longer than hard_max, keep the beginning and trim the end
    (never cut the fight opening to keep the peak).

    Returns (start_sec, end_sec, duration_sec).
    """
    min_d = _min_sec()
    max_d = _max_sec()
    hard_max = _hard_max_sec()
    lead = _lead_sec()
    peak = float(peak_sec)

    analysis = _analysis_for(vod)
    file_dur = float(analysis.get("duration", 0.0))
    if file_dur <= 0:
        from smart_video_editor import ffprobe_duration

        file_dur = float(ffprobe_duration(vod) or 0.0)
    if file_dur <= 0:
        start = max(0.0, peak - lead)
        end = start + min_d
        return round(start, 2), round(end, 2), round(min_d, 2)

    step = max(1.0, _bar_step_sec())
    keep = _bar_keep()
    # Scan enough to cover a full fight on either side of the peak
    t0 = max(0.0, peak - hard_max - 15.0)
    t1 = min(file_dur, peak + hard_max + 15.0)
    series = _boss_bar_series(vod, t0, t1, step=step)
    runs = _bar_runs(series, keep=keep, gap_tolerate=_gap_tolerate())
    chosen = _run_containing_peak(runs, peak)

    if chosen is None:
        # Fallback: fixed window that still starts before the peak
        start = max(0.0, peak - max(lead, min_d * 0.35))
        end = min(file_dur, start + min(max_d, 45.0))
        if end - start < min_d:
            end = min(file_dur, start + min_d)
            start = max(0.0, end - min_d)
        return round(start, 2), round(end, 2), round(max(0.0, end - start), 2)

    run_start, run_end, _mx = chosen
    # Start slightly before bar appears (approach / intro of fight)
    start = max(0.0, run_start - lead)
    end = min(file_dur, max(run_end + step, peak + lead))

    dur = end - start
    if dur < min_d:
        # Grow forward first (keep fight start), then backward if needed
        end = min(file_dur, start + min_d)
        if end - start < min_d:
            start = max(0.0, end - min_d)
        dur = end - start

    # Prefer keeping fight START when trimming long runs
    prefer_start = os.environ.get("GENSHIN_BOSS_FIGHT_PREFER_START", "1") == "1"
    cap = hard_max
    if dur > max_d and os.environ.get("GENSHIN_BOSS_FIGHT_TRIM_LONG", "0") == "1":
        cap = max_d

    if dur > cap:
        if prefer_start:
            # Keep opening of the fight; trim the tail (peak may be mid/late — OK)
            end = min(file_dur, start + cap)
            # If peak would fall outside, shift just enough to include peak+lead
            if peak + lead > end:
                end = min(file_dur, peak + lead)
                start = max(0.0, end - cap)
                # Still bias toward run_start if peak fits
                if run_start >= start and peak <= start + cap:
                    start = max(0.0, min(run_start - lead, peak + lead - cap))
                    end = min(file_dur, start + cap)
        else:
            start = max(0.0, peak - lead)
            end = min(file_dur, start + cap)
        dur = end - start

    return round(start, 2), round(end, 2), round(dur, 2)


def expand_clip_to_full_boss_fight(vod: Path, clip: dict) -> dict:
    """Rewrite clip start/duration to cover the full boss fight from the start."""
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
        "boss_fight_from_start": True,
    }
