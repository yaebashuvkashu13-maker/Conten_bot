#!/usr/bin/env python3
"""Soften PUBG/Standoff VOD gates after consecutive zero-send scans."""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Iterator

from mlbb_vod_adaptive_gate import (
    record_vod_outcome,
    should_notify_soften,
    streak_from_state,
    trailing_zero_streak,
)

DEFAULT_STREAK_THRESHOLD = 2

# Level 1: relax menu/HUD overlay + slightly lower gun/combat bars.
SHOOTER_SOFTEN_L1: dict[str, str] = {
    "VISUAL_MENU_OVERLAY_MAX": "0.58",
    "VISUAL_PUBG_MIN_FRAMES_PASS": "2",
    "HIGHLIGHT_PANN_GUN_MIN": "0.22",
    "HIGHLIGHT_PANN_INFERENCE_FLOOR": "0.15",
    "SMART_PUBG_MAX_CENTER_TEXT": "0.72",
    "SMART_STANDOFF_MAX_CENTER_TEXT": "0.22",
    "SMART_PUBG_MIN_CENTER_MOTION": "0.014",
    "SMART_STANDOFF_MIN_CENTER_MOTION": "0.012",
    "SMART_PUBG_MIN_GUNFIRE_DENSITY": "0.048",
    "SMART_STANDOFF_MIN_GUNFIRE_DENSITY": "0.048",
    "PUBG_POV_MIN_CENTER_MOTION": "0.020",
    "PUBG_PVP_MIN_ACTIVE_QUARTERS": "1",
    "VISUAL_PUBG_MIN_CENTER_EDGE": "0.022",
    "VISUAL_PUBG_MIN_WEAPON_EDGE": "0.014",
}

# Level 2: allow Metro highlight HUD, one good frame enough, POV gate off.
SHOOTER_SOFTEN_L2: dict[str, str] = {
    **SHOOTER_SOFTEN_L1,
    "VISUAL_MENU_OVERLAY_MAX": "0.78",
    "VISUAL_PUBG_MIN_FRAMES_PASS": "1",
    "HIGHLIGHT_PANN_GUN_MIN": "0.18",
    "HIGHLIGHT_PANN_INFERENCE_FLOOR": "0.12",
    "SMART_PUBG_MAX_CENTER_TEXT": "0.85",
    "SMART_STANDOFF_MAX_CENTER_TEXT": "0.28",
    "PUBG_POV_GATE": "0",
    "PUBG_POV_MIN_CENTER_MOTION": "0.012",
    "PUBG_COMBAT_PANN_MIN": "0.18",
    "VISUAL_PUBG_MIN_CENTER_EDGE": "0.018",
    "VISUAL_PUBG_MIN_WEAPON_EDGE": "0.010",
    "VISUAL_PUBG_MIN_HIT_FLASH": "0.0010",
    "PUBG_COMBAT_FRAMES_REQUIRED": "1",
    "PUBG_METRO_VOD_MIN_PROBES": "1",
    "PUBG_METRO_MAX_SKY_RATIO": "0.20",
    "PUBG_METRO_SEGMENT_RELAX": "1",
    "HIGHLIGHT_PANN_PREFILTER_MIN": "0.10",
}


def streak_threshold() -> int:
    raw = os.environ.get(
        "SHOOTER_VOD_ZERO_STREAK_SOFTEN",
        os.environ.get("MLBB_VOD_ZERO_STREAK_SOFTEN", str(DEFAULT_STREAK_THRESHOLD)),
    )
    return max(1, int(raw))


# Level 3: long zero streak — trust gun audio, skip run/loot owner heuristics.
SHOOTER_SOFTEN_L3: dict[str, str] = {
    **SHOOTER_SOFTEN_L2,
    "VISUAL_MENU_OVERLAY_MAX": "0.85",
    "PUBG_RELAX_OWNER_HEURISTICS": "1",
    "SMART_PUBG_MAX_RUN_MOTION": "0.30",
    "SMART_PUBG_MIN_GUNFIRE_DENSITY": "0.040",
    "PUBG_METRO_SEGMENT_TRUST_VOD": "1",
    "PUBG_METRO_TITLE_TRUST": "1",
    "PUBG_REJECT_BOT_FARM": "0",
    "PUBG_PVP_MIN_ACTIVE_QUARTERS": "1",
    "HIGHLIGHT_PANN_PREFILTER_MIN": "0.08",
    "HIGHLIGHT_PANN_GUN_MIN": "0.15",
    "HIGHLIGHT_PANN_INFERENCE_FLOOR": "0.10",
    "PUBG_COMBAT_PANN_MIN": "0.14",
    "PUBG_COMBAT_FRAMES_REQUIRED": "1",
    "VISUAL_PUBG_MIN_HIT_FLASH": "0.0008",
    "VISUAL_PUBG_MIN_WEAPON_EDGE": "0.008",
}

# Level 4: streak 10+ — trust PANNs gun audio, probe more windows, lower hook bar.
SHOOTER_SOFTEN_L4: dict[str, str] = {
    **SHOOTER_SOFTEN_L3,
    "SHOOTER_VOD_MAX_PANN_PROBE": "28",
    "HIGHLIGHT_MAX_STAGE1": "32",
    "HIGHLIGHT_PANN_PREFILTER_MIN": "0.06",
    "HIGHLIGHT_PANN_GUN_MIN": "0.12",
    "HIGHLIGHT_PANN_INFERENCE_FLOOR": "0.08",
    "PUBG_PANNS_TRUST_MIN": "0.28",
    "PUBG_RELAX_OWNER_HEURISTICS": "2",
    "PUBG_COMBAT_PANN_MIN": "0.12",
    "PUBG_COMBAT_FRAMES_REQUIRED": "1",
    "SMART_PUBG_MIN_GUNFIRE_DENSITY": "0.032",
    "SMART_PUBG_MAX_RUN_MOTION": "0.35",
    "VIRAL_SEGMENT_HOOK_MIN": "0.04",
    "VIRAL_COMBAT_HOOK_MIN": "0.02",
    "SHOOTER_VOD_MIN_CLIP_SCORE": "0.02",
    "VISUAL_PUBG_MIN_HIT_FLASH": "0.0005",
    "VISUAL_PUBG_MIN_WEAPON_EDGE": "0.006",
}


def soften_level(streak: int) -> int:
    need = streak_threshold()
    if streak < need:
        return 0
    if streak >= need + 8:
        return 4
    if streak >= need + 4:
        return 3
    if streak >= need + 1:
        return 2
    return 1


def overrides_for_level(level: int) -> dict[str, str]:
    if level <= 0:
        return {}
    if level >= 4:
        return dict(SHOOTER_SOFTEN_L4)
    if level >= 3:
        return dict(SHOOTER_SOFTEN_L3)
    if level >= 2:
        return dict(SHOOTER_SOFTEN_L2)
    return dict(SHOOTER_SOFTEN_L1)


def soften_summary(level: int) -> str:
    if level <= 0:
        return "strict"
    ov = overrides_for_level(level)
    menu = ov.get("VISUAL_MENU_OVERLAY_MAX", "?")
    frames = ov.get("VISUAL_PUBG_MIN_FRAMES_PASS", "?")
    pov = "off" if ov.get("PUBG_POV_GATE") == "0" else "on"
    panns = ov.get("SHOOTER_VOD_MAX_PANN_PROBE", "")
    extra = f" panns={panns}" if panns else ""
    return f"soft L{level} menu<={menu} frames>={frames} pov_gate={pov}{extra}"


def telegram_soften_notice(game: str, streak: int, level: int) -> str:
    g = game.strip().upper()
    return (
        f"⚙️ {g}: серия без клипов {streak}. Включаю {soften_summary(level)}.\n"
        f"Смягчаю menu/HUD и combat gate — пришлю первый проходящий кусок."
    )


def telegram_exhaust_notice(
    game: str, vod_id: str, *, level: int, streak: int, detail: str = ""
) -> str:
    g = game.strip().upper()
    base = f"⚠️ {g} {vod_id}: 0 клипов"
    if level > 0:
        msg = f"{base} (мягкий L{level}, серия={streak})"
    else:
        need = streak_threshold()
        msg = f"{base} — ещё {max(0, need - streak)} VOD до смягчения"
    if detail:
        msg += f"\n{detail[:140]}"
    return msg


def soft_max_peak_tries() -> int:
    return max(1, int(os.environ.get("SHOOTER_VOD_SOFT_MAX_PEAK_TRIES", "6")))


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


__all__ = [
    "adaptive_env",
    "record_vod_outcome",
    "should_notify_soften",
    "soft_max_peak_tries",
    "soften_level",
    "soften_summary",
    "streak_from_state",
    "streak_threshold",
    "telegram_exhaust_notice",
    "telegram_soften_notice",
    "trailing_zero_streak",
]
