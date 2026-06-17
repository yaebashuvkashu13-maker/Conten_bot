#!/usr/bin/env python3
"""Portrait vs landscape helpers for MLBB video pipeline."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import cv2
import numpy as np


def ffprobe_dimensions(path: Path) -> tuple[int, int]:
    proc = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height",
            "-of",
            "csv=p=0:s=x",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    line = (proc.stdout or "").strip().splitlines()
    if not line:
        return 0, 0
    parts = line[0].split("x")
    if len(parts) != 2:
        return 0, 0
    try:
        return int(parts[0]), int(parts[1])
    except ValueError:
        return 0, 0


def is_portrait_dimensions(width: int, height: int) -> bool:
    if width <= 0 or height <= 0:
        return False
    return height > width * float(os.environ.get("MLBB_PORTRAIT_RATIO", "1.05"))


def is_portrait_frame(frame: np.ndarray) -> bool:
    h, w = frame.shape[:2]
    return is_portrait_dimensions(w, h)


def is_portrait_video(path: Path) -> bool:
    w, h = ffprobe_dimensions(path)
    return is_portrait_dimensions(w, h)


def resize_for_analysis(frame: np.ndarray) -> np.ndarray:
    """Keep aspect — portrait frames must not be squashed to 16:9."""
    if is_portrait_frame(frame):
        return cv2.resize(frame, (180, 320))
    return cv2.resize(frame, (320, 180))


def resize_for_kill_ui(frame: np.ndarray) -> np.ndarray:
    if is_portrait_frame(frame):
        return cv2.resize(frame, (270, 480))
    return cv2.resize(frame, (480, 270))


def target_render_size(source: Path) -> tuple[int, int]:
    """Return (width, height) for ffmpeg scale/pad."""
    try:
        from smart_video_editor import TARGET_HEIGHT, TARGET_WIDTH
    except ImportError:
        TARGET_WIDTH, TARGET_HEIGHT = 1920, 1080

    if os.environ.get("MLBB_PORTRAIT_RENDER", "1") != "1":
        return int(TARGET_WIDTH), int(TARGET_HEIGHT)
    if is_portrait_video(source):
        return (
            int(os.environ.get("MLBB_PORTRAIT_WIDTH", "1080")),
            int(os.environ.get("MLBB_PORTRAIT_HEIGHT", "1920")),
        )
    return int(TARGET_WIDTH), int(TARGET_HEIGHT)
