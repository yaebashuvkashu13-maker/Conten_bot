#!/usr/bin/env python3
"""Shared Smart Edit environment defaults."""

from __future__ import annotations


def passthrough_audio_env() -> dict[str, str]:
    """Original game audio only — no aggressive DSP chain."""
    return {
        "SMART_GAME_AUDIO_ONLY": "0",
        "SMART_STRIP_MUSIC_BED": "0",
        "SMART_ADD_MUSIC": "0",
    }


def aggressive_action_base() -> dict[str, str]:
    """More clips, shorter windows, higher action density for overnight montages."""
    return {
        "MIN_HIGHLIGHTS": "5",
        "MAX_HIGHLIGHTS": "5",
        "MIN_FINAL_DURATION": "36",
        "MAX_FINAL_DURATION": "55",
        "SMART_BURST_WEIGHT": "0.48",
        "SMART_ACTION_CLIP_MIN_SEC": "7",
        "SMART_ACTION_CLIP_MAX_SEC": "10",
        "SMART_SKIP_INTRO_SEC": "90",
        "OVERNIGHT_AGGRESSIVE": "1",
    }


def pubg_combat_env() -> dict[str, str]:
    """PUBG Metro: gunshot audio, reject loot/walk/intro."""
    return {
        **aggressive_action_base(),
        "SMART_PUBG_PEAK_PERCENTILE": "36",
        "SMART_PUBG_SUSTAIN_PERCENTILE": "28",
        "SMART_PUBG_COMBAT_MIN": "0.20",
        "SMART_PUBG_BIN_GUNFIRE_MIN": "0.09",
        "SMART_PUBG_MIN_BIN_GUNFIRE_PEAK": "0.13",
        "SMART_PUBG_GUNFIRE_PEAK_PERCENTILE": "86",
        "SMART_PUBG_MIN_GUNFIRE_DENSITY": "0.075",
        "SMART_PUBG_MIN_BURST_RATIO": "3.2",
        "SMART_PUBG_MAX_TALK_RMS": "0.034",
        "SMART_PUBG_MAX_RUN_MOTION": "0.20",
        "SMART_PUBG_RELAX_MIN_GUNFIRE": "0.050",
        "SMART_PUBG_MAX_CENTER_TEXT": "0.62",
        "SMART_PUBG_MIN_CENTER_MOTION": "0.020",
        "SMART_PUBG_SKIP_INTRO_SEC": "120",
        "SMART_PUBG_CLIP_MIN_SEC": "7",
        "SMART_PUBG_CLIP_MAX_SEC": "9.5",
        "SMART_PUBG_MOTION_PERCENTILE": "48",
        "SMART_PUBG_AUDIO_PERCENTILE": "46",
        "SMART_PUBG_GUNFIRE_PERCENTILE": "56",
    }


def genshin_combat_env() -> dict[str, str]:
    """Genshin: boss fights only — HP bar + sustained combat, no trash mobs."""
    return {
        **aggressive_action_base(),
        "SMART_GENSHIN_REQUIRE_BOSS": "1",
        "SMART_GENSHIN_PEAK_PERCENTILE": "38",
        "SMART_GENSHIN_SUSTAIN_PERCENTILE": "30",
        "SMART_GENSHIN_COMBAT_MIN": "0.17",
        "SMART_GENSHIN_MOTION_PERCENTILE": "50",
        "SMART_GENSHIN_AUDIO_PERCENTILE": "48",
        "SMART_GENSHIN_SCENE_PERCENTILE": "52",
        "SMART_GENSHIN_MIN_CENTER_MOTION": "0.020",
        "SMART_GENSHIN_MIN_BOSS_BAR": "0.20",
        "SMART_GENSHIN_MIN_BOSS_BAR_PEAK": "0.28",
        "SMART_GENSHIN_MIN_BOSS_SCORE": "0.32",
        "SMART_GENSHIN_MIN_AUDIO_RMS": "0.012",
        "SMART_GENSHIN_MIN_CLUSTER_SEC": "18",
        "SMART_GENSHIN_MIN_SEGMENT_GAP": "90",
        "SMART_ALLOW_EXCLUDED_FALLBACK": "0",
        "SMART_ACTION_CLIP_MIN_SEC": "8",
        "SMART_ACTION_CLIP_MAX_SEC": "11",
    }


def standoff_combat_env() -> dict[str, str]:
    """Standoff 2: FPS duels — gunfire transients + aim motion."""
    return {
        **aggressive_action_base(),
        "SMART_STANDOFF_PEAK_PERCENTILE": "32",
        "SMART_STANDOFF_SUSTAIN_PERCENTILE": "26",
        "SMART_STANDOFF_COMBAT_MIN": "0.16",
        "SMART_STANDOFF_BIN_GUNFIRE_MIN": "0.07",
        "SMART_STANDOFF_MIN_GUNFIRE_DENSITY": "0.040",
        "SMART_STANDOFF_MIN_BURST_RATIO": "2.0",
        "SMART_STANDOFF_RELAX_MIN_GUNFIRE": "0.034",
        "SMART_STANDOFF_MIN_CENTER_MOTION": "0.018",
        "SMART_STANDOFF_MOTION_PERCENTILE": "48",
        "SMART_STANDOFF_AUDIO_PERCENTILE": "46",
        "SMART_STANDOFF_GUNFIRE_PERCENTILE": "54",
    }


def wot_combat_env() -> dict[str, str]:
    """WoT: hits/explosions/brawls — reject empty driving."""
    return {
        **aggressive_action_base(),
        "SMART_WOT_PEAK_PERCENTILE": "34",
        "SMART_WOT_SUSTAIN_PERCENTILE": "28",
        "SMART_WOT_COMBAT_MIN": "0.17",
        "SMART_WOT_BIN_IMPACT_MIN": "0.11",
        "SMART_WOT_MIN_IMPACT_DENSITY": "0.055",
        "SMART_WOT_MIN_BURST_RATIO": "2.4",
        "SMART_WOT_MIN_CLUSTER_SEC": "14",
        "SMART_WOT_MIN_CENTER_MOTION": "0.015",
        "SMART_WOT_MOTION_PERCENTILE": "46",
        "SMART_WOT_AUDIO_PERCENTILE": "52",
        "SMART_WOT_SCENE_PERCENTILE": "44",
        "SMART_WOT_IMPACT_PERCENTILE": "52",
    }


def mlbb_combat_env() -> dict[str, str]:
    """MLBB: only in-match fights, minimap required, no draft/webcam in frame."""
    return {
        **aggressive_action_base(),
        "STRICT_GAMEPLAY": "1",
        "SMART_REQUIRE_MINIMAP": "1",
        "SMART_MIN_MINIMAP_PRESENCE": "0.72",
        "SMART_MIN_MINIMAP_DELTA": "0.011",
        "SMART_MIN_CENTER_MOTION": "0.018",
        "SMART_MIN_HUD_FRAME_RATE": "0.72",
        "SMART_MAX_OVERLAY_TEXT": "0.10",
        "SMART_REJECT_DRAFT_QUEUE": "1",
        "SMART_CROP_WEBCAM": "1",
        "SMART_MLBB_PEAK_PERCENTILE": "52",
        "MIN_FINAL_DURATION": "42",
        "SMART_OUTPUT_CRF": "15",
        "SMART_OUTPUT_PRESET": "slow",
    }


def profile_montage_env(profile: str) -> dict[str, str]:
    """Per-game Smart Edit defaults."""
    combat_map = {
        "mobile_legends": mlbb_combat_env,
        "mlbb": mlbb_combat_env,
        "pubg": pubg_combat_env,
        "genshin": genshin_combat_env,
        "standoff": standoff_combat_env,
        "wot": wot_combat_env,
        "world_of_tanks": wot_combat_env,
    }
    if profile not in combat_map:
        return {}
    out = passthrough_audio_env()
    out.update(combat_map[profile]())
    return out


def relaxed_montage_env(profile: str) -> dict[str, str]:
    """Second-attempt floors — still action-heavy, never back to filler montages."""
    if profile in ("mobile_legends", "mlbb"):
        return {
            "SMART_MLBB_PEAK_PERCENTILE": "46",
            "SMART_MIN_MINIMAP_PRESENCE": "0.65",
            "SMART_MIN_CENTER_MOTION": "0.016",
            "MIN_HIGHLIGHTS": "4",
            "MAX_HIGHLIGHTS": "5",
        }
    if profile == "pubg":
        return {
            "SMART_PUBG_PEAK_PERCENTILE": "28",
            "SMART_PUBG_COMBAT_MIN": "0.14",
            "SMART_PUBG_BIN_GUNFIRE_MIN": "0.07",
            "SMART_PUBG_MIN_GUNFIRE_DENSITY": "0.058",
            "SMART_PUBG_RELAX_MIN_GUNFIRE": "0.055",
            "SMART_PUBG_SUSTAIN_PERCENTILE": "24",
            "MIN_HIGHLIGHTS": "4",
            "MAX_HIGHLIGHTS": "5",
        }
    if profile == "genshin":
        return {
            "SMART_GENSHIN_PEAK_PERCENTILE": "32",
            "SMART_GENSHIN_COMBAT_MIN": "0.15",
            "SMART_GENSHIN_SUSTAIN_PERCENTILE": "26",
            "SMART_GENSHIN_RELAX_MIN_BOSS_BAR": "0.16",
            "SMART_GENSHIN_MIN_BOSS_BAR_PEAK": "0.24",
            "SMART_GENSHIN_MIN_CENTER_MOTION": "0.018",
            "SMART_GENSHIN_MIN_CLUSTER_SEC": "14",
            "MIN_HIGHLIGHTS": "4",
            "MAX_HIGHLIGHTS": "5",
        }
    if profile == "standoff":
        return {
            "SMART_STANDOFF_PEAK_PERCENTILE": "24",
            "SMART_STANDOFF_COMBAT_MIN": "0.13",
            "SMART_STANDOFF_BIN_GUNFIRE_MIN": "0.055",
            "SMART_STANDOFF_MIN_GUNFIRE_DENSITY": "0.034",
            "SMART_STANDOFF_RELAX_MIN_GUNFIRE": "0.028",
            "MIN_HIGHLIGHTS": "4",
            "MAX_HIGHLIGHTS": "5",
        }
    if profile in ("wot", "world_of_tanks"):
        return {
            "SMART_WOT_PEAK_PERCENTILE": "28",
            "SMART_WOT_COMBAT_MIN": "0.13",
            "SMART_WOT_BIN_IMPACT_MIN": "0.08",
            "SMART_WOT_MIN_IMPACT_DENSITY": "0.042",
            "SMART_WOT_SUSTAIN_PERCENTILE": "24",
            "MIN_HIGHLIGHTS": "4",
            "MAX_HIGHLIGHTS": "5",
        }
    return {"MIN_HIGHLIGHTS": "4", "MAX_HIGHLIGHTS": "5"}
