#!/usr/bin/env python3
"""Soften PUBG/Standoff VOD gates after consecutive zero-send scans.

A+B+C drought policy:
- streak ladder L1/L2 (default max=2) eases numeric/visual combat bars
- never disables menu / loot / bot-farm hard rejects
- L2 enables reason-specific run_fake_gun rescue via drought PANNs + style-first rank
- pubg_drought_elasticity scales numeric floors −15%/idle-hour, +10% after send
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Iterator

from mlbb_vod_adaptive_gate import (
    record_vod_outcome as _mlbb_record_vod_outcome,
    should_notify_soften,
    streak_from_state,
    trailing_zero_streak,
)

DEFAULT_STREAK_THRESHOLD = 1

# Level 1: mild visual ease + style-first + slight combat numeric ease.
SHOOTER_SOFTEN_L1: dict[str, str] = {
    "VISUAL_PUBG_MIN_FRAMES_PASS": "2",
    "HIGHLIGHT_PANN_GUN_MIN": "0.20",
    "HIGHLIGHT_PANN_INFERENCE_FLOOR": "0.14",
    "SMART_PUBG_MAX_CENTER_TEXT": "0.72",
    "SMART_STANDOFF_MAX_CENTER_TEXT": "0.22",
    "SMART_PUBG_MIN_CENTER_MOTION": "0.014",
    "SMART_STANDOFF_MIN_CENTER_MOTION": "0.012",
    "PUBG_POV_MIN_CENTER_MOTION": "0.018",
    "PUBG_PVP_MIN_ACTIVE_QUARTERS": "1",
    "VISUAL_PUBG_MIN_CENTER_EDGE": "0.020",
    "VISUAL_PUBG_MIN_WEAPON_EDGE": "0.012",
    # Style-first during drought (FxTv-like fights float up).
    "PUBG_STYLE_RANK_BLEND": "0.68",
    "PUBG_AUTHOR_KILL_STYLE_COMBAT": "1",
    # Keep hard rejects pinned even if ambient env drifted.
    "PUBG_REJECT_BOT_FARM": "1",
    "PUBG_HARD_REJECT_MENU_OVERLAY": "1",
    "PUBG_REJECT_LOOT_WALK": "1",
}

# Level 2: reason-specific fake-gun rescue + stronger style bias. Still no menu/loot/bot waive.
SHOOTER_SOFTEN_L2: dict[str, str] = {
    **SHOOTER_SOFTEN_L1,
    "VISUAL_PUBG_MIN_FRAMES_PASS": "1",
    "HIGHLIGHT_PANN_GUN_MIN": "0.18",
    "HIGHLIGHT_PANN_INFERENCE_FLOOR": "0.12",
    "SMART_PUBG_MAX_CENTER_TEXT": "0.80",
    "SMART_STANDOFF_MAX_CENTER_TEXT": "0.26",
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
    "PUBG_STYLE_RANK_BLEND": "0.75",
    # Enable drought PANNs override path for run_fake_gun (shooting gate).
    "PUBG_ADAPTIVE_DROUGHT_RESCUE": "1",
    "PUBG_DROUGHT_PANNS_OVERRIDE": "0.40",
    "PUBG_DROUGHT_GUN_FACTOR": "0.65",
    "PUBG_STYLE_FAKE_GUN_OVERRIDE_MIN_GUN": "0.028",
    "PUBG_STYLE_FAKE_GUN_OVERRIDE_MIN_PANNS": "0.38",
    "PUBG_REJECT_BOT_FARM": "1",
    "PUBG_HARD_REJECT_MENU_OVERLAY": "1",
    "PUBG_REJECT_LOOT_WALK": "1",
}

# Level 3 (opt-in via MAX_SOFTEN>=3): more probes / audio trust — still NO bot-farm waive.
SHOOTER_SOFTEN_L3: dict[str, str] = {
    **SHOOTER_SOFTEN_L2,
    "PUBG_METRO_SEGMENT_TRUST_VOD": "1",
    "PUBG_METRO_TITLE_TRUST": "1",
    "PUBG_REJECT_BOT_FARM": "1",
    "PUBG_PVP_MIN_ACTIVE_QUARTERS": "1",
    "HIGHLIGHT_PANN_PREFILTER_MIN": "0.08",
    "HIGHLIGHT_PANN_GUN_MIN": "0.15",
    "HIGHLIGHT_PANN_INFERENCE_FLOOR": "0.10",
    "PUBG_COMBAT_PANN_MIN": "0.14",
    "PUBG_COMBAT_FRAMES_REQUIRED": "1",
    "VISUAL_PUBG_MIN_HIT_FLASH": "0.0008",
    "VISUAL_PUBG_MIN_WEAPON_EDGE": "0.008",
    "PUBG_STYLE_RANK_BLEND": "0.80",
    "PUBG_DROUGHT_PANNS_OVERRIDE": "0.38",
}

# Level 4 (opt-in): deeper audio trust / more probes. Never RELAX owner heuristics here.
SHOOTER_SOFTEN_L4: dict[str, str] = {
    **SHOOTER_SOFTEN_L3,
    "SHOOTER_VOD_MAX_PANN_PROBE": "28",
    "HIGHLIGHT_MAX_STAGE1": "32",
    "HIGHLIGHT_PANN_PREFILTER_MIN": "0.06",
    "HIGHLIGHT_PANN_GUN_MIN": "0.12",
    "HIGHLIGHT_PANN_INFERENCE_FLOOR": "0.08",
    "PUBG_PANNS_TRUST_MIN": "0.28",
    "PUBG_COMBAT_PANN_MIN": "0.12",
    "PUBG_COMBAT_FRAMES_REQUIRED": "1",
    "VIRAL_SEGMENT_HOOK_MIN": "0.04",
    "VIRAL_COMBAT_HOOK_MIN": "0.02",
    "SHOOTER_VOD_MIN_CLIP_SCORE": "0.02",
    "VISUAL_PUBG_MIN_HIT_FLASH": "0.0005",
    "VISUAL_PUBG_MIN_WEAPON_EDGE": "0.006",
    "PUBG_REJECT_BOT_FARM": "1",
    "PUBG_HARD_REJECT_MENU_OVERLAY": "1",
    "PUBG_REJECT_LOOT_WALK": "1",
}


def streak_threshold() -> int:
    raw = os.environ.get(
        "SHOOTER_VOD_ZERO_STREAK_SOFTEN",
        os.environ.get("MLBB_VOD_ZERO_STREAK_SOFTEN", str(DEFAULT_STREAK_THRESHOLD)),
    )
    return max(1, int(raw))


def soften_level(streak: int) -> int:
    """Adaptive soften ceiling — default max L2 keeps menu/loot/bot hard."""
    if os.environ.get("SHOOTER_VOD_DISABLE_SOFTEN", "0") == "1":
        return 0
    need = streak_threshold()
    if streak < need:
        return 0
    if streak >= need + 8:
        level = 4
    elif streak >= need + 4:
        level = 3
    elif streak >= need + 1:
        level = 2
    else:
        level = 1
    # Default max L2: reason-specific rescue without L3/L4 trash paths.
    max_level = int(os.environ.get("SHOOTER_VOD_MAX_SOFTEN_LEVEL", "2"))
    return max(0, min(level, max_level))


def overrides_for_level(level: int) -> dict[str, str]:
    if level <= 0:
        return {}
    if level >= 4:
        ov = dict(SHOOTER_SOFTEN_L4)
    elif level >= 3:
        ov = dict(SHOOTER_SOFTEN_L3)
    elif level >= 2:
        ov = dict(SHOOTER_SOFTEN_L2)
    else:
        ov = dict(SHOOTER_SOFTEN_L1)
    # Never let streak-soften re-enable owner-heuristic relax (talk/loot path).
    if os.environ.get("VOD_FORCE_SOFTEN", "0") != "1":
        ov["PUBG_RELAX_OWNER_HEURISTICS"] = "0"
    # Absolute hard locks.
    ov["PUBG_REJECT_BOT_FARM"] = "1"
    ov["PUBG_HARD_REJECT_MENU_OVERLAY"] = "1"
    ov.setdefault("PUBG_REJECT_LOOT_WALK", "1")
    return ov


def soften_summary(level: int) -> str:
    if level <= 0:
        return "strict"
    ov = overrides_for_level(level)
    frames = ov.get("VISUAL_PUBG_MIN_FRAMES_PASS", "?")
    text = ov.get("SMART_PUBG_MAX_CENTER_TEXT", "?")
    pov = "off" if ov.get("PUBG_POV_GATE") == "0" else "on"
    style = ov.get("PUBG_STYLE_RANK_BLEND", "")
    extra = f" style={style}" if style else ""
    return f"soft L{level} text<={text} frames>={frames} pov_gate={pov}{extra}"


def telegram_soften_notice(game: str, streak: int, level: int) -> str:
    g = game.strip().upper()
    return (
        f"⚙️ {g}: серия без клипов {streak}. Включаю {soften_summary(level)}.\n"
        f"Смягчаю combat/style (меню/лут/bot — нет) — пришлю первый проходящий кусок."
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
    if os.environ.get("PUBG_FULL_PEAK_SCAN", "1") == "1":
        raw = os.environ.get("SHOOTER_VOD_SOFT_MAX_PEAK_TRIES", "0")
        try:
            val = int(raw)
        except (TypeError, ValueError):
            val = 0
        if val <= 0:
            return 10_000
        return max(1, val)
    return max(1, int(os.environ.get("SHOOTER_VOD_SOFT_MAX_PEAK_TRIES", "6")))


def record_vod_outcome(state: dict, *, vod_id: str, sent: int) -> int:
    """Record VOD outcome; on send, harden elasticity +10% for the next hour."""
    streak = _mlbb_record_vod_outcome(state, vod_id=vod_id, sent=sent)
    if int(sent) > 0:
        try:
            from pubg_drought_elasticity import note_successful_send

            note_successful_send()
        except Exception:
            pass
    return streak


@contextmanager
def adaptive_env(streak: int) -> Iterator[int]:
    level = soften_level(streak)
    overrides = overrides_for_level(level)
    # Always run elasticity (even at L0) so multi-hour silence eases numeric floors.
    touch_keys = set(overrides)
    saved = {k: os.environ.get(k) for k in touch_keys}
    # Also remember elasticity-managed keys for restore.
    elastic_saved: dict[str, str | None] = {}
    flag_keys = (
        "PUBG_DROUGHT_ELASTICITY_ACTIVE",
        "PUBG_DROUGHT_ELASTICITY_SCALE",
        "PUBG_DROUGHT_ELASTICITY_IDLE_HOURS",
        "SHOOTER_VOD_SOFTEN_LEVEL",
        "PUBG_ADAPTIVE_DROUGHT_RESCUE",
    )
    for key in flag_keys:
        elastic_saved[key] = os.environ.get(key)
    try:
        if overrides:
            os.environ.update(overrides)
        os.environ["SHOOTER_VOD_SOFTEN_LEVEL"] = str(level)
        try:
            from pubg_drought_elasticity import ELASTIC_KEYS, apply_elasticity_to_environ

            for key in ELASTIC_KEYS:
                if key not in elastic_saved:
                    elastic_saved[key] = os.environ.get(key)
            apply_elasticity_to_environ()
        except Exception:
            pass
        try:
            from game_adaptive_thresholds import apply_to_environ

            apply_to_environ(
                os.environ.get("VOD_SEGMENT_GAME")
                or os.environ.get("SHOOTER_VOD_GAME")
                or "pubg"
            )
        except Exception:
            pass
        yield level
    finally:
        os.environ.pop("SHOOTER_VOD_SOFTEN_LEVEL", None)
        for key, prev in saved.items():
            if prev is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = prev
        for key, prev in elastic_saved.items():
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
