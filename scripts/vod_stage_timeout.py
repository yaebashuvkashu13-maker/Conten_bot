#!/usr/bin/env python3
"""Shared stage timeouts for VOD pipelines — no single step blocks forever."""

from __future__ import annotations

import os


def stage_timeout_sec(name: str, default: float) -> float:
    """Per-stage override: MLBB_VOD_<NAME>_TIMEOUT_SEC, else MLBB_VOD_STAGE_TIMEOUT_SEC."""
    specific = os.environ.get(f"MLBB_VOD_{name.upper()}_TIMEOUT_SEC", "").strip()
    if specific:
        return max(5.0, float(specific))
    generic = os.environ.get("MLBB_VOD_STAGE_TIMEOUT_SEC", "").strip()
    if generic:
        return max(5.0, float(generic))
    return max(5.0, float(default))


def analyze_timeout_sec() -> float:
    return stage_timeout_sec("analyze", 600.0)


def render_timeout_sec() -> float:
    return stage_timeout_sec("render", 300.0)


def ffmpeg_timeout_sec() -> float:
    return stage_timeout_sec("ffmpeg", 120.0)


def frame_extract_timeout_sec() -> float:
    return stage_timeout_sec("frame_extract", 30.0)


def presend_timeout_sec() -> float:
    return stage_timeout_sec("presend", 180.0)


def download_timeout_sec() -> float:
    raw = os.environ.get("YOUTUBE_DOWNLOAD_TIMEOUT", "").strip()
    if raw:
        return max(60.0, float(raw))
    return stage_timeout_sec("download", 1200.0)
