#!/usr/bin/env python3
"""Global PUBG Metro fight-act profile (owner-calibrated, applies to ALL VODs).

Derived from owner-marked acts on 6mWLqNBX1pE (2026-09-06): real fights often
sit at gun≈0.03–0.09 with burst≥5 and little/no OCR kill payoff. These rules
must apply to every video — not only labeled timestamps — so the owner does not
need to mark each VOD.
"""

from __future__ import annotations

import os
from typing import Any

# Numeric floors matching owner acts (global defaults).
ACT_MIN_GUN = 0.032
ACT_MIN_BURST = 4.5
ACT_BURST_ESCAPE = 5.5
ACT_BURST_ESCAPE_GUN = 0.028

# Frozen style fingerprint averaged toward owner style_ref acts on 6mWLqNBX1pE.
# Low payoff/notification is intentional — Metro acts are often OCR-blind.
GLOBAL_ACT_STYLE_PROFILE: dict[str, Any] = {
    "source": "owner_6mWLqNBX1pE_acts_2026-09-06",
    "peaks": [211.0, 234.0, 265.0, 287.0, 406.0, 430.0, 446.0],
    "gunfire_density": 0.068,
    "panns_gun_max": 0.42,
    "notification_score": 0.08,
    "payoff_fast": 0.06,
    "fight_fast": 0.58,
    "notification_hit": 0.15,
}


def combat_act_enabled() -> bool:
    return os.environ.get("PUBG_GLOBAL_FIGHT_ACT", "1") == "1"


def is_combat_act(
    gun: float,
    burst: float,
    *,
    min_gun: float | None = None,
    min_burst: float | None = None,
) -> bool:
    """True when audio matches owner-calibrated Metro fight acts."""
    if not combat_act_enabled():
        return False
    gun_floor = float(
        min_gun
        if min_gun is not None
        else os.environ.get("PUBG_FIGHT_ACT_MIN_GUN", str(ACT_MIN_GUN))
    )
    burst_floor = float(
        min_burst
        if min_burst is not None
        else os.environ.get("PUBG_FIGHT_ACT_MIN_BURST", str(ACT_MIN_BURST))
    )
    if gun >= gun_floor and burst >= burst_floor:
        return True
    # Strong burst escape (owner sprays while strafing).
    escape_burst = float(os.environ.get("PUBG_FAKE_GUN_BURST_ESCAPE", str(ACT_BURST_ESCAPE)))
    escape_gun = float(
        os.environ.get("PUBG_FAKE_GUN_BURST_ESCAPE_GUN", str(ACT_BURST_ESCAPE_GUN))
    )
    return burst >= escape_burst and gun >= escape_gun


def apply_global_act_defaults() -> None:
    """Install owner-act floors as process defaults for every VOD."""
    os.environ.setdefault("PUBG_GLOBAL_FIGHT_ACT", "1")
    os.environ.setdefault("PUBG_FIGHT_ACT_MIN_GUN", str(ACT_MIN_GUN))
    os.environ.setdefault("PUBG_FIGHT_ACT_MIN_BURST", str(ACT_MIN_BURST))
    os.environ.setdefault("PUBG_FAKE_GUN_BURST_ESCAPE", str(ACT_BURST_ESCAPE))
    os.environ.setdefault("PUBG_FAKE_GUN_BURST_ESCAPE_GUN", str(ACT_BURST_ESCAPE_GUN))
    # Always allow OCR-blind combat acts (not only near owner labels / drought).
    os.environ.setdefault("PUBG_COMBAT_ACT_PAYOFF_BYPASS", "1")
    os.environ.setdefault("PUBG_COMBAT_ACT_PAYOFF_FLOOR", "0.0")
    # New normal floors — owner acts must pass without per-VOD labels.
    os.environ.setdefault("PUBG_SINGLE_MIN_GUN_DENSITY", str(ACT_MIN_GUN))
    os.environ.setdefault("PUBG_CLIP_MIN_GUN_DENSITY", str(ACT_MIN_GUN))
    os.environ.setdefault("PUBG_PRESEND_MIN_GUN_DENSITY", str(ACT_MIN_GUN))
    os.environ.setdefault("PUBG_POOL_MIN_GUN_DENSITY", str(ACT_MIN_GUN))
    os.environ.setdefault("SHOOTER_VOD_DENSE_GUN_MIN", str(ACT_MIN_GUN))
    os.environ.setdefault("SMART_PUBG_MIN_GUNFIRE_DENSITY", "0.038")
    os.environ.setdefault("PUBG_CLIP_MIN_BURST_RATIO", str(ACT_MIN_BURST))
    os.environ.setdefault("PUBG_PAYOFF_SCORE_MIN_SINGLES", "0.10")
    os.environ.setdefault("PUBG_STYLE_COMBAT_MIN_GUN", str(ACT_MIN_GUN))
    os.environ.setdefault("PUBG_STYLE_COMBAT_MIN_BURST", str(ACT_MIN_BURST))
    os.environ.setdefault("PUBG_STYLE_USE_GLOBAL_ACT_PROFILE", "1")
