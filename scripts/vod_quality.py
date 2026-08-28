#!/usr/bin/env python3
"""Quality-first VOD pipeline flags (stricter gates, no soften shortcuts)."""

from __future__ import annotations

import os


def pubg_quality_strict() -> bool:
    """Strict PUBG Metro montage: full gates, ×3 parts, no adaptive soften."""
    explicit = os.environ.get("VOD_PUBG_QUALITY_STRICT", "").strip()
    if explicit == "1":
        return True
    if explicit == "0":
        return False
    # PUBG-only servers default to quality-first (not throughput shortcuts).
    return os.environ.get("VOD_PUBG_ONLY", "0") == "1"
