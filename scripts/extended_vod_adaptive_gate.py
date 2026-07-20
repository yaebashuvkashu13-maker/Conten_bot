#!/usr/bin/env python3
"""Soften Genshin / WoT VOD gates after consecutive zero-send scans."""

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

GENSHIN_SOFTEN_L1: dict[str, str] = {
    "VISUAL_MENU_OVERLAY_MAX": "0.58",
    "SMART_GENSHIN_STRICT_MIN_BOSS_SCORE": "0.30",
    "SMART_GENSHIN_STRICT_MIN_CENTER_MOTION": "0.14",
    "SMART_GENSHIN_COMBAT_MIN": "0.14",
}

GENSHIN_SOFTEN_L2: dict[str, str] = {
    **GENSHIN_SOFTEN_L1,
    "VISUAL_MENU_OVERLAY_MAX": "0.78",
    "SMART_GENSHIN_STRICT_MIN_BOSS_SCORE": "0.24",
    "SMART_GENSHIN_STRICT_MIN_CENTER_MOTION": "0.10",
    "SMART_GENSHIN_COMBAT_MIN": "0.12",
}

GENSHIN_SOFTEN_L3: dict[str, str] = {
    **GENSHIN_SOFTEN_L2,
    "SMART_GENSHIN_STRICT_MIN_BOSS_SCORE": "0.18",
    "SMART_GENSHIN_STRICT_MIN_CENTER_MOTION": "0.07",
    "PUBG_RELAX_OWNER_HEURISTICS": "1",
    "VIRAL_SEGMENT_HOOK_MIN": "0.05",
    "VIRAL_COMBAT_HOOK_MIN": "0.03",
}

GENSHIN_SOFTEN_L4: dict[str, str] = {
    **GENSHIN_SOFTEN_L3,
    "SMART_GENSHIN_STRICT_MIN_BOSS_SCORE": "0.14",
    "SMART_GENSHIN_STRICT_MIN_CENTER_MOTION": "0.05",
    "VIRAL_SEGMENT_HOOK_MIN": "0.03",
    "VIRAL_COMBAT_HOOK_MIN": "0.015",
    "HIGHLIGHT_EXTENDED_CLIP_HOOK_MIN": "0.08",
}

WOT_SOFTEN_L1: dict[str, str] = {
    "VISUAL_MENU_OVERLAY_MAX": "0.58",
    "SMART_WOT_MIN_IMPACT_DENSITY": "0.048",
    "SMART_WOT_MIN_BURST_RATIO": "2.0",
    "SMART_WOT_COMBAT_MIN": "0.14",
    "PUBG_COMBAT_FRAMES_REQUIRED": "2",
}

WOT_SOFTEN_L2: dict[str, str] = {
    **WOT_SOFTEN_L1,
    "VISUAL_MENU_OVERLAY_MAX": "0.78",
    "SMART_WOT_MIN_IMPACT_DENSITY": "0.042",
    "SMART_WOT_MIN_BURST_RATIO": "1.7",
    "PUBG_COMBAT_FRAMES_REQUIRED": "1",
    "VISUAL_PUBG_MIN_FRAMES_PASS": "1",
    "VIRAL_SEGMENT_HOOK_MIN": "0.06",
    "VIRAL_COMBAT_HOOK_MIN": "0.04",
}

WOT_SOFTEN_L3: dict[str, str] = {
    **WOT_SOFTEN_L2,
    "SMART_WOT_MIN_IMPACT_DENSITY": "0.036",
    "SMART_WOT_MIN_BURST_RATIO": "1.4",
    "PUBG_RELAX_OWNER_HEURISTICS": "1",
    "PUBG_REJECT_BOT_FARM": "0",
    "VIRAL_SEGMENT_HOOK_MIN": "0.04",
    "VIRAL_COMBAT_HOOK_MIN": "0.02",
    "WOT_BRAWL_MIN_HIT_FLASHES": "1",
}

WOT_SOFTEN_L4: dict[str, str] = {
    **WOT_SOFTEN_L3,
    "SMART_WOT_MIN_IMPACT_DENSITY": "0.028",
    "SMART_WOT_MIN_BURST_RATIO": "1.2",
    "WOT_BRAWL_GATE": "0",
    "VIRAL_SEGMENT_HOOK_MIN": "0.03",
    "VIRAL_COMBAT_HOOK_MIN": "0.015",
    "HIGHLIGHT_EXTENDED_CLIP_HOOK_MIN": "0.08",
    "EXTENDED_VOD_SOFT_MAX_PEAK_TRIES": "10",
}


def streak_threshold() -> int:
    raw = os.environ.get(
        "EXTENDED_VOD_ZERO_STREAK_SOFTEN",
        os.environ.get("SHOOTER_VOD_ZERO_STREAK_SOFTEN", str(DEFAULT_STREAK_THRESHOLD)),
    )
    return max(1, int(raw))


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


def overrides_for_level(game: str, level: int) -> dict[str, str]:
    if level <= 0:
        return {}
    g = game.strip().lower()
    if g == "genshin":
        tiers = (GENSHIN_SOFTEN_L1, GENSHIN_SOFTEN_L2, GENSHIN_SOFTEN_L3, GENSHIN_SOFTEN_L4)
    else:
        tiers = (WOT_SOFTEN_L1, WOT_SOFTEN_L2, WOT_SOFTEN_L3, WOT_SOFTEN_L4)
    if level >= 4:
        return dict(tiers[3])
    if level >= 3:
        return dict(tiers[2])
    if level >= 2:
        return dict(tiers[1])
    return dict(tiers[0])


def soften_summary(game: str, level: int) -> str:
    if level <= 0:
        return "strict"
    g = game.strip().lower()
    if g == "genshin":
        ov = overrides_for_level(game, level)
        return f"soft L{level} boss>={ov.get('SMART_GENSHIN_STRICT_MIN_BOSS_SCORE', '?')}"
    ov = overrides_for_level(game, level)
    return f"soft L{level} impact>={ov.get('SMART_WOT_MIN_IMPACT_DENSITY', '?')}"


def telegram_soften_notice(game: str, streak: int, level: int) -> str:
    g = game.strip().upper()
    return (
        f"⚙️ {g}: серия без клипов {streak}. Включаю {soften_summary(game, level)}.\n"
        f"Смягчаю boss/combat gate — пришлю первый проходящий кусок."
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
    return max(1, int(os.environ.get("EXTENDED_VOD_SOFT_MAX_PEAK_TRIES", "6")))


@contextmanager
def adaptive_env(game: str, streak: int) -> Iterator[int]:
    level = soften_level(streak)
    overrides = overrides_for_level(game, level)
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
    "overrides_for_level",
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
