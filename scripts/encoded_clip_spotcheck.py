#!/usr/bin/env python3
"""Spot-check encoded MP4 at start / middle / end for freeze, menu-ish stills, no gun audio."""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any


def _ffprobe_duration(path: Path) -> float:
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(path),
    ]
    try:
        out = subprocess.check_output(cmd, text=True, timeout=30).strip()
        return float(out or 0.0)
    except Exception:
        return 0.0


def _sample_points(duration: float) -> list[tuple[str, float]]:
    if duration <= 1.0:
        return [("mid", 0.0)]
    return [
        ("start", min(0.4, duration * 0.05)),
        ("mid", duration * 0.5),
        ("end", max(0.0, duration - 0.6)),
    ]


def _frame_stats(path: Path, at_sec: float) -> dict[str, float]:
    """Return crude still/menu proxies via ffmpeg signalstats."""
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        f"{max(0.0, at_sec):.3f}",
        "-i",
        str(path),
        "-frames:v",
        "2",
        "-vf",
        "signalstats,metadata=print:file=-",
        "-f",
        "null",
        "-",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=40)
    except (OSError, subprocess.TimeoutExpired):
        return {}
    text = (proc.stderr or "") + (proc.stdout or "")
    stats: dict[str, float] = {}
    for key in ("YAVG", "YMIN", "YMAX", "SATAVG"):
        for line in text.splitlines():
            if f"{key}=" in line:
                try:
                    stats[key.lower()] = float(line.split(f"{key}=")[-1].split()[0])
                except (TypeError, ValueError, IndexError):
                    pass
    return stats


def _audio_rms(path: Path, at_sec: float, dur: float = 0.8) -> float:
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        f"{max(0.0, at_sec):.3f}",
        "-t",
        f"{dur:.2f}",
        "-i",
        str(path),
        "-af",
        "astats=metadata=1:reset=1",
        "-f",
        "null",
        "-",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=40)
    except (OSError, subprocess.TimeoutExpired):
        return 0.0
    text = (proc.stderr or "") + (proc.stdout or "")
    for line in text.splitlines():
        if "RMS_level" in line and "=" in line:
            try:
                # dBFS — convert roughly to 0..1 linear-ish score
                db = float(line.split("=")[-1].strip().split()[0])
                return max(0.0, min(1.0, (db + 60.0) / 60.0))
            except (TypeError, ValueError):
                continue
    return 0.0


def spotcheck_encoded_clip(path: Path | str) -> tuple[bool, str, dict[str, Any]]:
    """
    Validate finished Telegram clip, not only the source window.
    Rejects: near-freeze across samples, menu-dark stills, silent/no-gun audio.
    """
    p = Path(path)
    if not p.exists() or p.stat().st_size < 10_000:
        return False, "missing_or_tiny", {}
    duration = _ffprobe_duration(p)
    if duration < float(os.environ.get("ENCODED_CLIP_MIN_SEC", "4")):
        return False, f"too_short:{duration:.2f}", {"duration": duration}

    freeze_yavg_delta_max = float(os.environ.get("ENCODED_CLIP_FREEZE_YAVG_DELTA", "1.5"))
    min_audio = float(os.environ.get("ENCODED_CLIP_MIN_AUDIO_RMS", "0.08"))
    samples: list[dict[str, Any]] = []
    yavgs: list[float] = []
    audio_scores: list[float] = []
    for name, t in _sample_points(duration):
        st = _frame_stats(p, t)
        rms = _audio_rms(p, t)
        samples.append({"at": name, "t": t, "frame": st, "audio_rms": rms})
        if "yavg" in st:
            yavgs.append(st["yavg"])
        audio_scores.append(rms)

    report = {"duration": duration, "samples": samples}
    if len(yavgs) >= 2:
        delta = max(yavgs) - min(yavgs)
        report["yavg_delta"] = delta
        if delta < freeze_yavg_delta_max and max(yavgs) < 25.0:
            return False, f"freeze_or_menu:yavg_delta={delta:.2f}", report
    if audio_scores and max(audio_scores) < min_audio:
        return False, f"no_audio_activity:max_rms={max(audio_scores):.3f}", report
    return True, "ok", report
