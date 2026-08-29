#!/usr/bin/env python3
"""Quality-first VOD pipeline flags (stricter gates, no soften shortcuts)."""

from __future__ import annotations

import os


def pubg_quality_strict() -> bool:
    """Strict PUBG Metro montage: full gates, ×3 parts, no adaptive soften."""
    explicit = os.environ.get("VOD_PUBG_QUALITY_STRICT", "").strip()
    if explicit == "1":
        return True
    # Delivery-first by default — strict mode is opt-in via VOD_PUBG_QUALITY_STRICT=1.
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
