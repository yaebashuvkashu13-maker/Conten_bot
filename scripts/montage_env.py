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


def profile_montage_env(profile: str) -> dict[str, str]:
    """MLBB/PUBG: user prefers source audio without the combat-DSP filter chain."""
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
