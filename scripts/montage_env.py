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


def mlbb_combat_env() -> dict[str, str]:
    """MLBB: only in-match fights, minimap required, no draft/webcam in frame."""
    return {
        "STRICT_GAMEPLAY": "1",
        "SMART_REQUIRE_MINIMAP": "1",
        "SMART_MIN_MINIMAP_PRESENCE": "0.72",
        "SMART_MIN_MINIMAP_DELTA": "0.011",
        "SMART_MIN_CENTER_MOTION": "0.018",
        "SMART_MIN_HUD_FRAME_RATE": "0.72",
        "SMART_MAX_OVERLAY_TEXT": "0.10",
        "SMART_REJECT_DRAFT_QUEUE": "1",
        "SMART_CROP_WEBCAM": "1",
        "SMART_MLBB_PEAK_PERCENTILE": "54",
        "MIN_HIGHLIGHTS": "5",
        "MAX_HIGHLIGHTS": "5",
        "MIN_FINAL_DURATION": "42",
        "MAX_FINAL_DURATION": "57",
        "SMART_OUTPUT_CRF": "15",
        "SMART_OUTPUT_PRESET": "slow",
    }


def profile_montage_env(profile: str) -> dict[str, str]:
    """Per-game Smart Edit defaults."""
    if profile in (
        "mobile_legends",
        "pubg",
        "mlbb",
        "genshin",
        "standoff",
        "wot",
        "world_of_tanks",
    ):
        out = passthrough_audio_env()
        if profile in ("mobile_legends", "mlbb"):
            out.update(mlbb_combat_env())
        if profile == "pubg":
            out.update(
                {
                    "MIN_HIGHLIGHTS": "5",
                    "MAX_HIGHLIGHTS": "5",
                    "SMART_PUBG_PEAK_PERCENTILE": "38",
                    "SMART_PUBG_SUSTAIN_PERCENTILE": "30",
                    "SMART_PUBG_COMBAT_MIN": "0.14",
                    "SMART_BURST_WEIGHT": "0.46",
                    "SMART_PUBG_CLIP_MIN_SEC": "7",
                    "SMART_PUBG_CLIP_MAX_SEC": "10",
                }
            )
        return out
    return {}
