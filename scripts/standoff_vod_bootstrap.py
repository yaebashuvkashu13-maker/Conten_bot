#!/usr/bin/env python3
"""Standoff 2 cold-start — relaxed gates until exemplars + 👍 feedback accumulate."""

from __future__ import annotations

import logging
import os

log = logging.getLogger("standoff_bootstrap")

BOOTSTRAP_ENV: dict[str, str] = {
    "PUBG_RELAX_OWNER_HEURISTICS": "2",
    "PUBG_PRESEND_COMBAT_FAST": "1",
    "SMART_STANDOFF_MIN_GUNFIRE_DENSITY": "0.038",
    "SMART_STANDOFF_MIN_BURST_RATIO": "3.5",
    "SMART_STANDOFF_MIN_CENTER_MOTION": "0.022",
    "SMART_PUBG_MIN_GUNFIRE_DENSITY": "0.038",
    "SMART_PUBG_MIN_BURST_RATIO": "3.5",
    "SMART_PUBG_MAX_RUN_MOTION": "0.32",
    "PUBG_PRESEND_MIN_GUN_DENSITY": "0.028",
    "PUBG_KILLFEED_PRESEND_MIN": "0.10",
    "SHOOTER_VOD_MIN_CLIP_SCORE": "0.01",
    "HIGHLIGHT_PANN_GUN_MIN": "0.14",
    "HIGHLIGHT_PANN_INFERENCE_FLOOR": "0.08",
    "PUBG_COMBAT_PANN_MIN": "0.12",
}


def bootstrap_min_exemplars() -> int:
    return max(1, int(os.environ.get("STANDOFF_BOOTSTRAP_MIN_EXEMPLARS", "5")))


def standoff_bootstrap_active() -> bool:
    if os.environ.get("STANDOFF_VOD_BOOTSTRAP", "1") != "1":
        return False
    try:
        from vod_owner_learning import exemplar_counts

        good_n, _ = exemplar_counts("standoff")
    except Exception:
        good_n = 0
    return good_n < bootstrap_min_exemplars()


def apply_standoff_bootstrap_env() -> bool:
    """Apply cold-start overrides. Returns True when active."""
    if not standoff_bootstrap_active():
        return False
    for key, val in BOOTSTRAP_ENV.items():
        os.environ[key] = val
    log.info(
        "standoff bootstrap active (good_exemplars<%s) — relaxed combat/presend gates",
        bootstrap_min_exemplars(),
    )
    return True
