#!/usr/bin/env python3
"""PUBG fight-boundary segmentation — variable clip while gunfire sustains."""

from __future__ import annotations

import os
from pathlib import Path


def _enabled() -> bool:
    return os.environ.get("PUBG_VOD_VARIABLE_LENGTH", "1") == "1"


def normalize_pubg_clip(clip: dict, vod: Path) -> dict:
    """Extend montage window while combat sustains (no fixed 8s cap)."""
    if not _enabled():
        return clip
    peak = float(clip.get("peak_start", clip.get("start", 0)))
    from mlbb_fight_segment import detect_fight_bounds

    min_sec = float(os.environ.get("PUBG_FIGHT_MIN_SEC", os.environ.get("MLBB_FIGHT_MIN_SEC", "8")))
    max_sec = float(os.environ.get("PUBG_FIGHT_MAX_SEC", os.environ.get("MLBB_FIGHT_MAX_SEC", "45")))
    hard_max = float(os.environ.get("PUBG_FIGHT_HARD_MAX_SEC", os.environ.get("MLBB_FIGHT_HARD_MAX_SEC", "55")))
    saved = {
        "MLBB_FIGHT_MIN_SEC": os.environ.get("MLBB_FIGHT_MIN_SEC"),
        "MLBB_FIGHT_MAX_SEC": os.environ.get("MLBB_FIGHT_MAX_SEC"),
        "MLBB_FIGHT_HARD_MAX_SEC": os.environ.get("MLBB_FIGHT_HARD_MAX_SEC"),
    }
    try:
        os.environ["MLBB_FIGHT_MIN_SEC"] = str(min_sec)
        os.environ["MLBB_FIGHT_MAX_SEC"] = str(max_sec)
        os.environ["MLBB_FIGHT_HARD_MAX_SEC"] = str(hard_max)
        start, end, dur = detect_fight_bounds(vod, peak)
    finally:
        for key, val in saved.items():
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val
    return {
        **clip,
        "start": start,
        "peak_start": peak,
        "fight_end": end,
        "input_duration": dur,
        "output_duration": dur,
        "source_path": str(vod),
    }
