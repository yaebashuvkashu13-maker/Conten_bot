#!/usr/bin/env python3
"""Persist drought soften knobs for systemd feed restarts.

Force-send / hang-recover apply soften only to a child env. Bare
``systemctl start`` then reloads ``.video_bot.env`` (SOFTEN=0) and the long-lived
feed rejects every clip until the next heal. Write a second EnvironmentFile that
the unit loads *after* the steady pins so drought floors survive the hand-off.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping

# Keys that must survive systemd resume under drought.
OVERLAY_KEYS: tuple[str, ...] = (
    "VOD_FORCE_SOFTEN",
    "VOD_FORCE_ESCALATION",
    "VOD_FORCE_QUALITY_MIN",
    "VOD_FORCE_PAYOFF_MIN",
    "VOD_FORCE_GUN_DENSITY",
    "VOD_FORCE_BURST_RATIO",
    "VOD_FORCE_SKIP_DISCOVERY",
    "SHOOTER_VOD_SKIP_DISCOVERY",
    "PUBG_QUALITY_SCORE_MIN_SINGLES",
    "PUBG_PAYOFF_SCORE_MIN_SINGLES",
    "PUBG_SINGLE_MIN_GUN_DENSITY",
    "PUBG_CLIP_MIN_GUN_DENSITY",
    "PUBG_CLIP_MIN_BURST_RATIO",
    "PUBG_PRESEND_MIN_GUN_DENSITY",
    "PUBG_POOL_MIN_GUN_DENSITY",
    "SMART_PUBG_MIN_GUNFIRE_DENSITY",
    "SHOOTER_VOD_DENSE_GUN_MIN",
    "DISLIKE_GUN_DENSITY_MIN",
    "DISLIKE_BURST_RATIO_MIN",
    "DISLIKE_MENU_OVERLAY_MAX",
    "CLIP_HOOK_MAX_MENU",
    "CLIP_HOOK_MIN_AUDIO_RMS",
    "CLIP_HOOK_MIN_YAVG_DELTA",
    "PUBG_DISLIKE_LOOT_FLOOR_LOCK",
    "PUBG_DISLIKE_REQUIRE_KILL_EVIDENCE",
    "PUBG_REQUIRE_AUTHOR_KILL_SINGLES",
    "PUBG_REQUIRE_AUTHOR_KILL",
    "SHOOTER_REQUIRE_AUTHOR_KILL",
    "PUBG_OWNER_GOOD_TRUST_NO_KILL",
    "PUBG_COMBAT_ACT_ALLOW_NO_KILL",
    "PUBG_RELAX_OWNER_HEURISTICS",
    "PUBG_STYLE_AVOID_ENABLE",
    "VOD_PUBG_QUALITY_STRICT",
    "PUBG_FAST_RANK_DROP_LOOT_WALK",
    "PUBG_SINGLES_ZERO_SEND_EXHAUST",
    "PUBG_OWNER_NEIGHBORHOOD_GUN_SNAP",
    "PUBG_SINGLES_GUN_PAYOFF_BYPASS",
    "PUBG_SINGLES_GUN_QUALITY_BYPASS",
    "PUBG_REJECT_LOOT_WALK",
    "PUBG_PRESEND_SHOOTING_GATE",
    "PUBG_FULL_PEAK_SCAN",
    "PUBG_COMBAT_TIMELINE",
)


def overlay_path() -> Path:
    return Path(
        os.environ.get(
            "VOD_DROUGHT_ENV_FILE",
            "/root/.video_bot.drought.env",
        )
    )


def write_drought_overlay(source: Mapping[str, str] | None = None) -> Path:
    """Write drought EnvironmentFile from *source* (or os.environ)."""
    src = source if source is not None else os.environ
    soften = str(src.get("VOD_FORCE_SOFTEN", os.environ.get("VOD_FORCE_SOFTEN", "0")))
    if soften != "1":
        clear_drought_overlay()
        return overlay_path()
    path = overlay_path()
    lines = [
        "# Auto-written by vod_drought_overlay — do not edit by hand.",
        "# Loaded after .video_bot.env so drought floors survive systemctl start.",
        "VOD_FORCE_SOFTEN=1",
    ]
    for key in OVERLAY_KEYS:
        if key == "VOD_FORCE_SOFTEN":
            continue
        raw = src.get(key)
        if raw is None or str(raw).strip() == "":
            continue
        # Escape minimal shell-unsafe chars for EnvironmentFile.
        val = str(raw).replace("\n", " ").replace('"', '\\"')
        lines.append(f'{key}="{val}"')
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    tmp.replace(path)
    return path


def clear_drought_overlay() -> None:
    path = overlay_path()
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def drought_overlay_active() -> bool:
    path = overlay_path()
    if not path.is_file():
        return False
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return False
    return "VOD_FORCE_SOFTEN=1" in text or 'VOD_FORCE_SOFTEN="1"' in text


__all__ = [
    "OVERLAY_KEYS",
    "clear_drought_overlay",
    "drought_overlay_active",
    "overlay_path",
    "write_drought_overlay",
]
