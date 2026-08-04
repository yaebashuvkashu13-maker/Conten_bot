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
}

# Soften must LOWER barriers vs production defaults. Absolute "0.048" values used to
# overwrite a looser env (e.g. 0.012) and accidentally harden the gate during a dry streak.
WOT_SOFTEN_L1: dict[str, str] = {
    "VISUAL_MENU_OVERLAY_MAX": "0.58",
    "SMART_WOT_MIN_IMPACT_DENSITY": "0.010",
    "SMART_WOT_CRUISE_IMPACT_CAP": "0.025",
    "WOT_BRAWL_CRUISE_IMPACT_MAX": "0.025",
    "WOT_BRAWL_MIN_HIT_FLASHES": "1",
    "SMART_WOT_MIN_BURST_RATIO": "1.6",
    "SMART_WOT_COMBAT_MIN": "0.12",
    "PUBG_COMBAT_FRAMES_REQUIRED": "2",
}

WOT_SOFTEN_L2: dict[str, str] = {
    **WOT_SOFTEN_L1,
    "VISUAL_MENU_OVERLAY_MAX": "0.78",
    "SMART_WOT_MIN_IMPACT_DENSITY": "0.006",
    "SMART_WOT_CRUISE_IMPACT_CAP": "0.015",
    "WOT_BRAWL_CRUISE_IMPACT_MAX": "0.015",
    "SMART_WOT_MIN_BURST_RATIO": "1.3",
    "PUBG_COMBAT_FRAMES_REQUIRED": "1",
    "VISUAL_PUBG_MIN_FRAMES_PASS": "1",
}

WOT_SOFTEN_L3: dict[str, str] = {
    **WOT_SOFTEN_L2,
    "SMART_WOT_MIN_IMPACT_DENSITY": "0.003",
    "SMART_WOT_CRUISE_IMPACT_CAP": "0.008",
    "WOT_BRAWL_CRUISE_IMPACT_MAX": "0.008",
    "SMART_WOT_MIN_BURST_RATIO": "1.1",
    "PUBG_RELAX_OWNER_HEURISTICS": "1",
    "PUBG_REJECT_BOT_FARM": "0",
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
    if streak >= need + 4:
        return 3
    if streak >= need + 1:
        return 2
    return 1


def overrides_for_level(game: str, level: int) -> dict[str, str]:
    if level <= 0:
        return {}
    g = game.strip().lower()
    tiers = (
        (GENSHIN_SOFTEN_L1, GENSHIN_SOFTEN_L2, GENSHIN_SOFTEN_L3)
        if g == "genshin"
        else (WOT_SOFTEN_L1, WOT_SOFTEN_L2, WOT_SOFTEN_L3)
    )
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
