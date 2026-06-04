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
    if profile in ("mobile_legends", "pubg", "mlbb", "genshin", "standoff", "wot"):
        return passthrough_audio_env()
    return {}
