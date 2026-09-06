#!/usr/bin/env python3
"""Quality-first VOD pipeline flags (stricter gates, no soften shortcuts)."""

from __future__ import annotations

import os


def pubg_quality_strict() -> bool:
    """Strict PUBG Metro montage: full gates, ×3 parts, denser probe.

    Does **not** disable drought elasticity or the safe L1/L2 adaptive ladder —
    those still ease numeric combat floors / run_fake_gun rescue while menu/loot/bot
    stay hard. Opt-in via VOD_PUBG_QUALITY_STRICT=1.
    """
    explicit = os.environ.get("VOD_PUBG_QUALITY_STRICT", "").strip()
    if explicit == "1":
        return True
    # Delivery-first by default — strict mode is opt-in via VOD_PUBG_QUALITY_STRICT=1.
    return False


def pubg_delivery_mode() -> bool:
    """Throughput mode: ship clips even when montage gates are picky."""
    if os.environ.get("SHOOTER_VOD_DELIVERY_FIRST", "0") == "1":
        return True
    if os.environ.get("VOD_PUBG_ONLY", "0") == "1" and not pubg_quality_strict():
        return True
    return False


def montages_per_vod(game: str = "pubg") -> int:
    """How many ×N склейки to ship from one VOD visit before moving on."""
    raw = os.environ.get("SHOOTER_VOD_MONTAGES_PER_VOD", "").strip()
    if raw:
        return max(1, int(raw))
    if game == "pubg" and pubg_quality_strict():
        return 3
    return 1


def dense_probe_passes() -> int:
    """Rotate dense PANNs windows across long VODs (same gates, more coverage)."""
    raw = os.environ.get("SHOOTER_VOD_DENSE_PROBE_PASSES", "").strip()
    if raw:
        return max(1, int(raw))
    if pubg_quality_strict():
        return 2
    return 1
