#!/usr/bin/env python3
"""Hook gate: first 1–2 seconds must already show contact (gun/motion), not approach/menu."""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except ValueError:
        return default


def _audio_rms(path: Path, start: float, dur: float) -> float:
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        f"{max(0.0, start):.3f}",
        "-t",
        f"{max(0.2, dur):.2f}",
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
                db = float(line.split("=")[-1].strip().split()[0])
                return max(0.0, min(1.0, (db + 60.0) / 60.0))
            except (TypeError, ValueError):
                continue
    return 0.0


def _frame_yavg(path: Path, at_sec: float) -> float:
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
        "1",
        "-vf",
        "signalstats,metadata=print:file=-",
        "-f",
        "null",
        "-",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return 0.0
    text = (proc.stderr or "") + (proc.stdout or "")
    for line in text.splitlines():
        if "YAVG=" in line:
            try:
                return float(line.split("YAVG=")[-1].split()[0])
            except (TypeError, ValueError, IndexError):
                return 0.0
    return 0.0


def _overlay_proxy(path: Path, at_sec: float) -> float:
    """Cheap menu proxy: extract tiny frame, score bright UI text density via signalstats SAT."""
    with tempfile.TemporaryDirectory(prefix="hook_") as td:
        jpg = Path(td) / "f.jpg"
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
            "1",
            "-vf",
            "scale=160:-2,signalstats,metadata=print:file=-",
            "-f",
            "null",
            "-",
        ]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            return 0.0
        text = (proc.stderr or "") + (proc.stdout or "")
        sat = 0.0
        yavg = 0.0
        for line in text.splitlines():
            if "SATAVG=" in line:
                try:
                    sat = float(line.split("SATAVG=")[-1].split()[0])
                except (TypeError, ValueError, IndexError):
                    pass
            if "YAVG=" in line:
                try:
                    yavg = float(line.split("YAVG=")[-1].split()[0])
                except (TypeError, ValueError, IndexError):
                    pass
        # High luma + low sat often means UI/menu panels.
        if yavg >= 90 and sat <= 18:
            return min(1.0, (yavg - 70.0) / 80.0)
        return max(0.0, min(1.0, (110.0 - sat) / 200.0 if yavg > 100 else 0.0))


def hook_gate_clip(
    path: Path | str,
    *,
    window_sec: float | None = None,
) -> tuple[bool, str, dict[str, Any]]:
    """
    Require early contact: audio activity + non-menu frame in the opening seconds.
    Rejects approach/loot openings and static menus.
    """
    p = Path(path)
    if not p.exists() or p.stat().st_size < 5_000:
        return False, "hook_missing", {}

    window = float(window_sec if window_sec is not None else _env_float("CLIP_HOOK_WINDOW_SEC", 1.8))
    min_rms = _env_float("CLIP_HOOK_MIN_AUDIO_RMS", 0.10)
    min_y_delta = _env_float("CLIP_HOOK_MIN_YAVG_DELTA", 2.0)
    max_menu = _env_float("CLIP_HOOK_MAX_MENU", 0.55)

    t0 = 0.15
    t1 = max(0.4, window * 0.55)
    t2 = max(0.8, window * 0.95)
    rms0 = _audio_rms(p, t0, min(0.7, window))
    rms1 = _audio_rms(p, t1, min(0.7, window))
    y0 = _frame_yavg(p, t0)
    y1 = _frame_yavg(p, t1)
    y2 = _frame_yavg(p, t2)
    menu0 = _overlay_proxy(p, t0)
    menu1 = _overlay_proxy(p, t1)

    y_delta = max(y0, y1, y2) - min(y0, y1, y2)
    max_rms = max(rms0, rms1)
    max_menu_score = max(menu0, menu1)
    report: dict[str, Any] = {
        "window_sec": window,
        "rms": [rms0, rms1],
        "yavg": [y0, y1, y2],
        "y_delta": y_delta,
        "menu": [menu0, menu1],
        "max_rms": max_rms,
        "max_menu": max_menu_score,
    }

    if max_menu_score >= max_menu:
        return False, f"hook_menu={max_menu_score:.3f}", report
    if max_rms < min_rms and y_delta < min_y_delta:
        return False, f"hook_no_contact:rms={max_rms:.3f}:ydelta={y_delta:.2f}", report
    if max_rms < min_rms * 0.5:
        return False, f"hook_silent:rms={max_rms:.3f}", report
    return True, "ok", report
