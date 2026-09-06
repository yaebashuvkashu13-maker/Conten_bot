#!/usr/bin/env python3
"""Cheap VOD cascade: audio + low-res frames first; only top peaks get heavy OCR/CLIP/render."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        return default


def cheap_peak_score(
    vod: Path,
    peak_sec: float,
    *,
    audio_fn: Callable[[Path, float, float], dict[str, Any]] | None = None,
    frame_fn: Callable[[Path, float], Path | None] | None = None,
) -> dict[str, Any]:
    """Score one peak with cached audio window + one low-res frame presence."""
    from vod_media_cache import cached_audio_window, extract_frame_cached

    def _default_audio(p: Path, start: float, dur: float) -> dict[str, Any]:
        # Lightweight RMS/crest via ffmpeg when no PANNs callback provided.
        import subprocess

        cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            f"{max(0.0, start):.3f}",
            "-t",
            f"{max(0.5, dur):.2f}",
            "-i",
            str(p),
            "-af",
            "astats=metadata=1:reset=1",
            "-f",
            "null",
            "-",
        ]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=45)
        except (OSError, subprocess.TimeoutExpired):
            return {"rms": 0.0, "gun_proxy": 0.0}
        text = (proc.stderr or "") + (proc.stdout or "")
        rms = 0.0
        peak = 0.0
        for line in text.splitlines():
            if "RMS_level" in line and "=" in line:
                try:
                    db = float(line.split("=")[-1].strip().split()[0])
                    rms = max(0.0, min(1.0, (db + 60.0) / 60.0))
                except (TypeError, ValueError):
                    pass
            if "Peak_level" in line and "=" in line:
                try:
                    db = float(line.split("=")[-1].strip().split()[0])
                    peak = max(0.0, min(1.0, (db + 60.0) / 60.0))
                except (TypeError, ValueError):
                    pass
        return {"rms": rms, "peak": peak, "gun_proxy": max(rms, peak * 0.9)}

    win = float(_env_float("CHEAP_CASCADE_AUDIO_WIN", 2.5))
    start = max(0.0, float(peak_sec) - win * 0.35)
    audio = cached_audio_window(vod, start, win, audio_fn or _default_audio)
    frame = None
    try:
        frame = (frame_fn or extract_frame_cached)(vod, float(peak_sec), width=240)
    except TypeError:
        frame = (frame_fn or extract_frame_cached)(vod, float(peak_sec))
    gun = float(audio.get("gun_proxy") or audio.get("gun") or audio.get("rms") or 0.0)
    score = gun
    if frame is not None:
        score += 0.05
    return {
        "peak_sec": float(peak_sec),
        "score": float(score),
        "gun_proxy": gun,
        "audio": audio,
        "frame": str(frame) if frame else "",
    }


def rank_peaks_cheap(
    vod: Path,
    peaks: list[float],
    *,
    top_k: int | None = None,
    min_gun: float | None = None,
    audio_fn: Callable[[Path, float, float], dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Filter + rank peaks before expensive OCR/CLIP/render."""
    if not peaks:
        return []
    # 0 / unset under full scan = keep every peak that clears min_gun.
    if top_k is None:
        if os.environ.get("PUBG_FULL_PEAK_SCAN", "1") == "1":
            top_k = _env_int("CHEAP_CASCADE_TOP_K", 0)
        else:
            top_k = _env_int("CHEAP_CASCADE_TOP_K", 8)
    else:
        top_k = int(top_k)
    min_gun = float(min_gun if min_gun is not None else _env_float("CHEAP_CASCADE_MIN_GUN", 0.08))
    scored = [cheap_peak_score(vod, float(p), audio_fn=audio_fn) for p in peaks]
    scored.sort(key=lambda r: float(r.get("score") or 0.0), reverse=True)
    kept = [r for r in scored if float(r.get("gun_proxy") or 0.0) >= min_gun]
    if not kept:
        # Keep best few anyway so feed can still attempt owner peaks.
        keep_n = len(scored) if top_k <= 0 else max(1, min(3, top_k))
        kept = scored[:keep_n]
    if top_k <= 0:
        return kept
    return kept[:top_k]


def should_run_heavy(candidate: dict[str, Any], *, rank: int) -> bool:
    """Whether OCR/CLIP/render is worth it for this cheap-ranked candidate.

    Under PUBG_FULL_PEAK_SCAN every peak that clears the cheap gun floor is
    inspected — no silent top-5 heavy shortlist.
    """
    if os.environ.get("PUBG_FULL_PEAK_SCAN", "1") == "1":
        hard_top = _env_int("CHEAP_CASCADE_HEAVY_TOP", 0)
        min_score = _env_float("CHEAP_CASCADE_HEAVY_MIN_SCORE", 0.0)
        if hard_top > 0 and rank >= hard_top:
            return False
        return float(candidate.get("score") or 0.0) >= min_score
    hard_top = _env_int("CHEAP_CASCADE_HEAVY_TOP", 5)
    min_score = _env_float("CHEAP_CASCADE_HEAVY_MIN_SCORE", 0.10)
    if rank >= hard_top:
        return False
    return float(candidate.get("score") or 0.0) >= min_score
