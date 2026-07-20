#!/usr/bin/env python3
"""Expand WoT highlight peaks to the full brawl window.

Discovery scores a short peak (~10s). This module finds the contiguous
hit-flash / impact run that contains the peak and cuts from the brawl START
(not mid-exchange) — same practice as genshin_boss_fight.

Env:
  WOT_BRAWL_FULL_FIGHT=1 / SHOOTER_VOD_VARIABLE_LENGTH=1
  WOT_BRAWL_FIGHT_MIN_SEC=18
  WOT_BRAWL_FIGHT_MAX_SEC=55
  WOT_BRAWL_FIGHT_HARD_MAX_SEC=75
  WOT_VOD_LEAD_SEC=4
  WOT_BRAWL_FIGHT_STEP_SEC=1.0
  WOT_BRAWL_FLASH_KEEP=0.003
  WOT_BRAWL_EDGE_KEEP=0.024
  WOT_BRAWL_GAP_TOLERATE=3
  WOT_BRAWL_FIGHT_PREFER_START=1
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

_CACHE: dict[str, dict] = {}


def _min_sec() -> float:
    return float(os.environ.get("WOT_BRAWL_FIGHT_MIN_SEC", "18"))


def _max_sec() -> float:
    return float(os.environ.get("WOT_BRAWL_FIGHT_MAX_SEC", "55"))


def _hard_max_sec() -> float:
    return float(os.environ.get("WOT_BRAWL_FIGHT_HARD_MAX_SEC", "75"))


def _lead_sec() -> float:
    return float(os.environ.get("WOT_VOD_LEAD_SEC", "4"))


def _step_sec() -> float:
    return max(0.5, float(os.environ.get("WOT_BRAWL_FIGHT_STEP_SEC", "1.0")))


def _flash_keep() -> float:
    return float(os.environ.get("WOT_BRAWL_FLASH_KEEP", "0.003"))


def _edge_keep() -> float:
    return float(os.environ.get("WOT_BRAWL_EDGE_KEEP", "0.024"))


def _gap_tolerate() -> int:
    return max(0, int(os.environ.get("WOT_BRAWL_GAP_TOLERATE", "3")))


def variable_length_enabled() -> bool:
    return os.environ.get(
        "WOT_BRAWL_FULL_FIGHT",
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


def _combat_series(
    vod: Path,
    t0: float,
    t1: float,
    *,
    step: float,
) -> list[tuple[float, float, float]]:
    """Batch-decode (t, hit_flash, center_edge) over [t0,t1]."""
    import numpy as np

    from visual_action_check import check_frame_visual

    span = max(0.0, t1 - t0)
    if span < 1.0:
        from gameplay_gate import _read_frame_at

        frame = _read_frame_at(vod, t0)
        if frame is None:
            return [(t0, 0.0, 0.0)]
        _ok, _r, m = check_frame_visual("wot", frame)
        return [(t0, float(m.get("hit_flash") or 0.0), float(m.get("center_edge") or 0.0))]

    fps = 1.0 / max(0.5, step)
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
    except Exception:
        return []
    if proc.returncode != 0 or not proc.stdout:
        return []

    out: list[tuple[float, float, float]] = []
    raw = proc.stdout
    idx = 0
    while True:
        offset = idx * frame_bytes
        chunk = raw[offset : offset + frame_bytes]
        if len(chunk) < frame_bytes:
            break
        frame = np.frombuffer(chunk, dtype=np.uint8).reshape((h, w, 3)).copy()
        t = float(t0) + float(idx) * step
        _ok, _r, m = check_frame_visual("wot", frame)
        out.append(
            (
                t,
                float(m.get("hit_flash") or 0.0),
                float(m.get("center_edge") or 0.0),
            )
        )
        idx += 1
    return out


def _active(flash: float, edge: float) -> bool:
    return flash >= _flash_keep() or edge >= _edge_keep()


def _run_containing_peak(
    series: list[tuple[float, float, float]],
    peak: float,
) -> tuple[float, float] | None:
    """Contiguous active run that contains peak (with gap tolerance)."""
    if not series:
        return None
    flash_k = _flash_keep()
    edge_k = _edge_keep()
    gap_tol = _gap_tolerate()

    runs: list[tuple[float, float]] = []
    run_start: float | None = None
    run_end: float | None = None
    gaps = 0
    for t, flash, edge in series:
        on = flash >= flash_k or edge >= edge_k
        if on:
            if run_start is None:
                run_start = t
            run_end = t
            gaps = 0
        elif run_start is not None:
            gaps += 1
            if gaps > gap_tol:
                runs.append((run_start, float(run_end if run_end is not None else run_start)))
                run_start = None
                run_end = None
                gaps = 0
            else:
                run_end = t
    if run_start is not None and run_end is not None:
        runs.append((run_start, run_end))

    if not runs:
        return None
    for a, b in runs:
        if a - 2.0 <= peak <= b + 2.0:
            return a, b
    # Nearest run by peak distance
    best = min(runs, key=lambda ab: min(abs(peak - ab[0]), abs(peak - ab[1])))
    return best


def detect_brawl_bounds(vod: Path, peak: float) -> tuple[float, float, float]:
    """Return (start, end, dur) for brawl containing peak. Prefer fight start."""
    from smart_video_editor import ffprobe_duration

    file_dur = float(ffprobe_duration(vod) or 0.0)
    if file_dur <= 5.0:
        return 0.0, 0.0, 0.0

    lead = _lead_sec()
    min_d = _min_sec()
    max_d = _max_sec()
    hard_max = _hard_max_sec()
    step = _step_sec()

    # Search window around peak (analysis gunfire helps widen when available)
    pad = max(hard_max, 45.0)
    t0 = max(0.0, peak - pad)
    t1 = min(file_dur, peak + pad)

    try:
        analysis = _analysis_for(vod)
        win = float(analysis.get("window_seconds", 2.0))
        gun = analysis.get("gunfire", analysis.get("audio"))
        if gun is not None:
            import numpy as np

            g = np.asarray(gun, dtype=np.float32)
            i_peak = int(peak / win)
            # Walk outward while gunfire stays elevated
            thr = float(np.percentile(g, 60)) if g.size else 0.02
            lo, hi = i_peak, i_peak
            while lo > 0 and float(g[lo]) >= thr * 0.55:
                lo -= 1
            while hi < len(g) - 1 and float(g[hi]) >= thr * 0.55:
                hi += 1
            t0 = max(0.0, min(t0, lo * win - 5.0))
            t1 = min(file_dur, max(t1, hi * win + 5.0))
    except Exception:
        pass

    series = _combat_series(vod, t0, t1, step=step)
    run = _run_containing_peak(series, peak)
    if run is None:
        # Fallback: fixed window from slightly before peak
        start = max(0.0, peak - lead)
        end = min(file_dur, start + min_d)
        return round(start, 2), round(end, 2), round(end - start, 2)

    run_start, run_end = run
    start = max(0.0, run_start - lead)
    end = min(file_dur, run_end + 1.5)
    dur = end - start

    if dur < min_d:
        end = min(file_dur, start + min_d)
        if end - start < min_d:
            start = max(0.0, end - min_d)
        dur = end - start

    prefer_start = os.environ.get("WOT_BRAWL_FIGHT_PREFER_START", "1") == "1"
    cap = hard_max
    if dur > max_d and os.environ.get("WOT_BRAWL_FIGHT_TRIM_LONG", "0") == "1":
        cap = max_d

    if dur > cap:
        if prefer_start:
            end = min(file_dur, start + cap)
            if peak + lead > end:
                end = min(file_dur, peak + lead)
                start = max(0.0, end - cap)
                if run_start >= start and peak <= start + cap:
                    start = max(0.0, min(run_start - lead, peak + lead - cap))
                    end = min(file_dur, start + cap)
        else:
            start = max(0.0, peak - lead)
            end = min(file_dur, start + cap)
        dur = end - start

    return round(start, 2), round(end, 2), round(dur, 2)


def expand_clip_to_full_brawl(vod: Path, clip: dict) -> dict:
    """Rewrite clip start/duration to cover the brawl from its start."""
    if not variable_length_enabled():
        return clip
    peak = float(clip.get("peak_start", clip.get("start", 0)))
    start, end, dur = detect_brawl_bounds(vod, peak)
    if dur <= 0:
        return clip
    return {
        **clip,
        "start": start,
        "peak_start": peak,
        "fight_end": end,
        "input_duration": dur,
        "output_duration": dur,
        "wot_brawl_full": True,
        "wot_brawl_dur": dur,
        "wot_brawl_from_start": True,
    }


__all__ = [
    "clear_analysis_cache",
    "detect_brawl_bounds",
    "expand_clip_to_full_brawl",
    "variable_length_enabled",
]
