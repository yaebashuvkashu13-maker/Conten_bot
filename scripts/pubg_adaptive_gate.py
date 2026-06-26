#!/usr/bin/env python3
"""Soften PUBG combat gates after consecutive VODs with zero montage output."""

from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

STATE_PATH = Path("/root/data/mlbb/pubg_adaptive_gate_state.json")
DEFAULT_STREAK_THRESHOLD = 3

# Level 1: relaxed gunfire + PANNs + visual (2/3 frames).
SOFTEN_L1: dict[str, str] = {
    "SMART_PUBG_MIN_GUNFIRE_DENSITY": "0.055",
    "SMART_PUBG_MIN_BURST_RATIO": "4.8",
    "SMART_PUBG_PEAK_PERCENTILE": "32",
    "SMART_PUBG_COMBAT_MIN": "0.18",
    "SMART_PUBG_RELAX_MIN_GUNFIRE": "0.050",
    "PUBG_COMBAT_PANN_MIN": "0.18",
    "HIGHLIGHT_PANN_GUN_MIN": "0.18",
    "HIGHLIGHT_PANN_PREFILTER_MIN": "0.08",
    "HIGHLIGHT_CLIP_MIN_SHOOTER": "0.08",
    "VISUAL_PUBG_MIN_FRAMES_PASS": "2",
    "PUBG_COMBAT_FRAMES_REQUIRED": "2",
    "PUBG_COMBAT_MIN_HIT_FLASH": "0.003",
    "PUBG_COMBAT_MIN_WEAPON_EDGE": "0.020",
    "PUBG_REJECT_BOT_FARM": "0",
    "VIRAL_HOOK_MIN": "0.32",
    "VIRAL_SEGMENT_HOOK_MIN": "0.28",
}

# Level 2: motion-first fights; skip bot-farm heuristics, lower audio floors.
SOFTEN_L2: dict[str, str] = {
    **SOFTEN_L1,
    "SMART_PUBG_MIN_GUNFIRE_DENSITY": "0.048",
    "SMART_PUBG_MIN_BURST_RATIO": "4.0",
    "PUBG_COMBAT_PANN_MIN": "0.14",
    "HIGHLIGHT_PANN_GUN_MIN": "0.14",
    "HIGHLIGHT_PANN_PREFILTER_MIN": "0.06",
    "HIGHLIGHT_CLIP_MIN_SHOOTER": "0.06",
    "PUBG_COMBAT_MIN_HIT_FLASH": "0.002",
    "PUBG_COMBAT_MIN_WEAPON_EDGE": "0.015",
    "PUBG_PVP_MIN_ACTIVE_QUARTERS": "1",
    "PUBG_PVP_MIN_BURST_CLUSTERS": "1",
    "PUBG_PVP_MIN_CENTER_MOTION": "0.022",
    "SMART_PUBG_MAX_CENTER_TEXT": "0.75",
}


def streak_threshold() -> int:
    return max(1, int(os.environ.get("PUBG_ZERO_STREAK_SOFTEN", str(DEFAULT_STREAK_THRESHOLD))))


def soften_level(streak: int) -> int:
    need = streak_threshold()
    if streak < need:
        return 0
    if streak >= need + 3:
        return 2
    return 1


def overrides_for_level(level: int) -> dict[str, str]:
    if level <= 0:
        return {}
    if level >= 2:
        return dict(SOFTEN_L2)
    return dict(SOFTEN_L1)


def trailing_zero_streak(results: list[dict]) -> int:
    n = 0
    for row in reversed(results):
        if int(row.get("clips", 0)) > 0:
            break
        n += 1
    return n


def load_state() -> dict:
    if not STATE_PATH.exists():
        return {"vod_outcomes": [], "zero_clip_streak": 0}
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except (json.JSONDecodeError, OSError):
        pass
    return {"vod_outcomes": [], "zero_clip_streak": 0}


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def streak_from_state(state: dict) -> int:
    hist = state.get("vod_outcomes")
    if isinstance(hist, list) and hist:
        return trailing_zero_streak(hist)
    return int(state.get("zero_clip_streak") or 0)


def record_vod_outcome(state: dict, *, vod_id: str, clips: int) -> int:
    hist = list(state.get("vod_outcomes") or [])
    hist.append({"id": vod_id, "clips": int(clips), "ts": time.strftime("%Y-%m-%dT%H:%M:%S")})
    state["vod_outcomes"] = hist[-40:]
    streak = trailing_zero_streak(state["vod_outcomes"])
    state["zero_clip_streak"] = streak
    return streak


def soften_summary(level: int) -> str:
    if level <= 0:
        return "strict"
    ov = overrides_for_level(level)
    gun = ov.get("SMART_PUBG_MIN_GUNFIRE_DENSITY", "?")
    pann = ov.get("PUBG_COMBAT_PANN_MIN", "?")
    frames = ov.get("PUBG_COMBAT_FRAMES_REQUIRED", "?")
    bot = "off" if ov.get("PUBG_REJECT_BOT_FARM") == "0" else "on"
    return f"soft L{level} gun>={gun} pann>={pann} vis={frames}/3 bot_farm={bot}"


def should_notify_soften(streak: int, level: int, *, prev_level: int) -> bool:
    if level <= 0:
        return False
    if level > prev_level:
        return True
    need = streak_threshold()
    if level == 1 and streak == need and prev_level == 0:
        return True
    if level == 2 and streak == need + 3 and prev_level < 2:
        return True
    return False


@contextmanager
def adaptive_env(streak: int) -> Iterator[int]:
    level = soften_level(streak)
    overrides = overrides_for_level(level)
    if not overrides:
        yield 0
        return
    saved = {k: os.environ.get(k) for k in overrides}
    try:
        os.environ.update(overrides)
        yield level
    finally:
        for key, prev in saved.items():
            if prev is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = prev


def merge_soften_into_env(env: dict[str, str], level: int) -> dict[str, str]:
    if level <= 0:
        return env
    merged = dict(env)
    merged.update(overrides_for_level(level))
    return merged
