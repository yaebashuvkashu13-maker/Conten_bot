#!/usr/bin/env python3
"""Validate downloaded YouTube MP4 and remux to readable H.264 when needed."""

from __future__ import annotations

import subprocess
from pathlib import Path


def video_readable(path: Path, *, t_sec: float = 1.0) -> bool:
    probe = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            f"{max(0.0, t_sec):.3f}",
            "-i",
            str(path),
            "-frames:v",
            "1",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        check=False,
        timeout=30,
    )
    return probe.returncode == 0


def remux_h264(path: Path) -> Path | None:
    tmp = path.with_suffix(".fixed.mp4")
    proc = subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-hwaccel",
            "none",
            "-i",
            str(path),
            "-map",
            "0:v:0",
            "-map",
            "0:a:0?",
            "-vf",
            "scale=-2:720",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-crf",
            "23",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-movflags",
            "+faststart",
            str(tmp),
        ],
        capture_output=True,
        check=False,
        timeout=600,
    )
    if proc.returncode != 0 or not tmp.exists() or not video_readable(tmp):
        tmp.unlink(missing_ok=True)
        return None
    path.unlink(missing_ok=True)
    tmp.rename(path)
    return path


def ensure_readable(path: Path) -> bool:
    if video_readable(path):
        return True
    return remux_h264(path) is not None
