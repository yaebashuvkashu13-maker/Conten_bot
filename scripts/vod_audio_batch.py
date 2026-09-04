#!/usr/bin/env python3
"""Single-pass VOD PCM extract + cheap DSP gun scoring before PANNs."""

from __future__ import annotations

import logging
import os
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np

log = logging.getLogger("vod_audio_batch")

GUN_SAMPLE_RATE = 11025
GUN_FRAME = 256


def batch_enabled() -> bool:
    return os.environ.get("SHOOTER_VOD_AUDIO_BATCH", "1") == "1"


def panns_top_n() -> int:
    try:
        from vod_scan_cascade import cascade_limits

        return max(8, cascade_limits().panns)
    except Exception:
        return max(8, int(os.environ.get("SHOOTER_VOD_PANN_TOP_N", "40")))


def extract_vod_pcm_s16(
    video_path: Path,
    start_sec: float,
    duration_sec: float,
    *,
    sample_rate: int = GUN_SAMPLE_RATE,
) -> np.ndarray:
    """One ffmpeg decode for the usable body of a VOD."""
    if duration_sec <= 0.35:
        return np.array([], dtype=np.int16)
    cmd = [
        "ffmpeg",
        "-v",
        "error",
        "-hwaccel",
        "none",
        "-ss",
        f"{max(0.0, float(start_sec)):.3f}",
        "-t",
        f"{max(0.35, float(duration_sec)):.3f}",
        "-i",
        str(video_path),
        "-vn",
        "-ac",
        "1",
        "-ar",
        str(sample_rate),
        "-f",
        "s16le",
        "-",
    ]
    timeout = max(60, int(duration_sec * 0.15) + 30)
    try:
        result = subprocess.run(cmd, capture_output=True, check=False, timeout=timeout)
    except subprocess.TimeoutExpired:
        return np.array([], dtype=np.int16)
    if result.returncode != 0 or not result.stdout:
        return np.array([], dtype=np.int16)
    return np.frombuffer(result.stdout, dtype=np.int16)


def pcm_to_float(pcm: np.ndarray) -> np.ndarray:
    if pcm.size == 0:
        return np.array([], dtype=np.float32)
    return pcm.astype(np.float32) / 32768.0


def gunfire_metrics_from_pcm(
    pcm_float: np.ndarray,
    sample_rate: int,
    window_start: float,
    window_sec: float,
    *,
    pcm_base_sec: float = 0.0,
) -> tuple[float, float, float]:
    """Same algorithm as gameplay_gate.score_pubg_gunfire_audio — density, burst, rms."""
    rel_start = float(window_start) - float(pcm_base_sec)
    i0 = max(0, int(rel_start * sample_rate))
    i1 = min(len(pcm_float), int((rel_start + window_sec) * sample_rate))
    segment = pcm_float[i0:i1]
    if segment.size < 384:
        return 0.0, 0.0, 0.0
    energies: list[float] = []
    for offset in range(0, len(segment) - GUN_FRAME, GUN_FRAME):
        chunk = segment[offset : offset + GUN_FRAME]
        energies.append(float(np.sqrt(np.mean(chunk * chunk))))
    if len(energies) < 3:
        return 0.0, 0.0, 0.0
    arr = np.asarray(energies, dtype=np.float32)
    median = float(np.median(arr))
    peak = float(np.max(arr))
    rms = float(np.mean(arr))
    floor = max(median * 2.6, 0.010)
    spikes = 0
    for idx in range(1, len(arr)):
        if arr[idx] > floor and arr[idx] > arr[idx - 1] * 1.55:
            spikes += 1
    density = spikes / max(len(arr) - 1, 1)
    burst_ratio = peak / max(rms, 1e-6)
    return density, burst_ratio, rms


def dsp_score_offsets(
    offsets: list[float],
    window_sec: float,
    pcm_float: np.ndarray,
    *,
    pcm_base_sec: float,
    sample_rate: int = GUN_SAMPLE_RATE,
) -> list[tuple[float, float, float, float]]:
    """Return [(offset, density, burst, rms), ...] without extra ffmpeg calls."""
    out: list[tuple[float, float, float, float]] = []
    for t in offsets:
        density, burst, rms = gunfire_metrics_from_pcm(
            pcm_float,
            sample_rate,
            t,
            window_sec,
            pcm_base_sec=pcm_base_sec,
        )
        out.append((float(t), density, burst, rms))
    return out


def _dsp_rank(row: tuple[float, float, float, float]) -> float:
    _offset, density, burst, rms = row
    return float(density) * 2.0 + float(burst) * 0.01 + float(rms) * 0.5


def _parallel_workers() -> int:
    raw = (os.environ.get("SHOOTER_VOD_PANN_WORKERS") or os.environ.get("HIGHLIGHT_PARALLEL_WORKERS") or "").strip()
    if raw:
        return max(1, int(raw))
    cpus = os.cpu_count() or 4
    return max(2, min(6, cpus - 2, int(cpus * 0.75)))


def panns_score_windows(
    video_path: Path,
    offsets: list[float],
    window_sec: float,
    *,
    gun_min: float,
) -> list[tuple[float, float]]:
    """Run PANNs on selected offsets (parallel when workers > 1)."""
    from highlight_scorer import score_panns_audio

    if not offsets:
        return []

    def _one(t: float) -> tuple[float, float] | None:
        panns = score_panns_audio(video_path, t, window_sec)
        gmax = float(panns.get("panns_gun_max", 0) or 0)
        if gmax >= gun_min:
            return gmax, t + window_sec * 0.5
        return None

    workers = _parallel_workers()
    scored: list[tuple[float, float]] = []
    if workers > 1 and len(offsets) > 1:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for hit in pool.map(_one, offsets):
                if hit is not None:
                    scored.append(hit)
    else:
        for t in offsets:
            hit = _one(t)
            if hit is not None:
                scored.append(hit)
    scored.sort(key=lambda x: -x[0])
    return scored


def discover_scored_windows(
    video_path: Path,
    offsets: list[float],
    window_sec: float,
    *,
    gun_min: float,
    pcm_float: np.ndarray | None = None,
    pcm_base_sec: float = 0.0,
) -> tuple[list[tuple[float, float]], dict[str, float | int]]:
    """
    DSP-first peak discovery helper.

    Returns PANNs-scored windows [(panns, center_hint), ...] and timing/meta stats.
    """
    stats: dict[str, float | int] = {
        "offsets": len(offsets),
        "dsp_pass": 0,
        "panns_windows": 0,
        "cache_hits": 0,
        "extract_ms": 0.0,
        "dsp_ms": 0.0,
        "panns_ms": 0.0,
    }
    if not offsets:
        return [], stats

    top_n = min(panns_top_n(), len(offsets))
    dsp_min = float(os.environ.get("SHOOTER_VOD_DSP_GUN_MIN", "0.018"))

    if batch_enabled() and pcm_float is None:
        t0 = time.perf_counter()
        store = None
        try:
            from vod_feature_store import open_store

            store = open_store(video_path, skip_intro=float(pcm_base_sec))
        except Exception:
            store = None
        pcm = np.array([], dtype=np.int16)
        if store is not None:
            need = max(0.0, offsets[-1] + window_sec + 4.0 - pcm_base_sec)
            if store.ensure_pcm(need):
                pcm = store.get_pcm_s16(copy=False)
                try:
                    store.put_features(
                        {
                            "pcm_sample_rate": GUN_SAMPLE_RATE,
                            "pcm_base_sec": float(pcm_base_sec),
                            "dsp_offsets": int(len(offsets)),
                        }
                    )
                except Exception:
                    pass
        if pcm.size == 0:
            pcm = extract_vod_pcm_s16(
                video_path,
                pcm_base_sec,
                max(0.0, offsets[-1] + window_sec + 4.0 - pcm_base_sec),
            )
        stats["extract_ms"] = round((time.perf_counter() - t0) * 1000.0, 1)
        pcm_float = pcm_to_float(pcm)

    if batch_enabled() and pcm_float is not None and pcm_float.size > 0:
        t0 = time.perf_counter()
        dsp_rows = dsp_score_offsets(
            offsets,
            window_sec,
            pcm_float,
            pcm_base_sec=pcm_base_sec,
        )
        stats["dsp_ms"] = round((time.perf_counter() - t0) * 1000.0, 1)
        dsp_rows.sort(key=_dsp_rank, reverse=True)
        dsp_pass = [row for row in dsp_rows if row[1] >= dsp_min]
        stats["dsp_pass"] = len(dsp_pass)
        panns_targets = [row[0] for row in (dsp_pass or dsp_rows)[:top_n]]
    else:
        panns_targets = offsets[:top_n]
        stats["dsp_pass"] = len(offsets)

    stats["panns_windows"] = len(panns_targets)
    t0 = time.perf_counter()
    scored = panns_score_windows(video_path, panns_targets, window_sec, gun_min=gun_min)
    stats["panns_ms"] = round((time.perf_counter() - t0) * 1000.0, 1)
    return scored, stats


class VodPcmCache:
    """In-memory PCM slice for snap_peak without per-window ffmpeg."""

    __slots__ = ("pcm_float", "sample_rate", "base_sec")

    def __init__(self, pcm_float: np.ndarray, *, base_sec: float, sample_rate: int = GUN_SAMPLE_RATE):
        self.pcm_float = pcm_float
        self.base_sec = float(base_sec)
        self.sample_rate = int(sample_rate)

    def gunfire_metrics(self, start_sec: float, duration_sec: float) -> tuple[float, float, float]:
        return gunfire_metrics_from_pcm(
            self.pcm_float,
            self.sample_rate,
            start_sec,
            duration_sec,
            pcm_base_sec=self.base_sec,
        )
