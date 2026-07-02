#!/usr/bin/env python3
"""PUBG/shooter clip timing: peak lag so combat is centered, not cut too early."""

from __future__ import annotations

import os


def peak_lag_sec(game: str) -> float:
    """
    Seconds to shift combat peak later before cutting (start/end move together).

    Owner feedback: PUBG highlights were cut ~10s before the real fight.
    """
    game = game.strip().lower()
    generic = os.environ.get("SHOOTER_VOD_PEAK_LAG_SEC", "").strip()
    if generic:
        return max(0.0, float(generic))
    if game == "pubg":
        return max(0.0, float(os.environ.get("PUBG_VOD_PEAK_LAG_SEC", "10")))
    if game == "standoff":
        return max(0.0, float(os.environ.get("STANDOFF_VOD_PEAK_LAG_SEC", "6")))
    return 0.0


def lead_sec(game: str) -> float:
    return float(os.environ.get("MLBB_VOD_LEAD_SEC", "4"))


def window_times(
    game: str,
    peak: float,
    *,
    duration: float | None = None,
) -> tuple[float, float, float]:
    """
    Return (start, peak_effective, duration).

    peak_effective = peak + lag; start = peak_effective - lead.
    """
    lag = peak_lag_sec(game)
    lead = lead_sec(game)
    peak_eff = float(peak) + lag
    start = max(0.0, peak_eff - lead)
    dur = float(duration if duration is not None else os.environ.get("HIGHLIGHT_WINDOW_SEC", "10"))
    return start, peak_eff, dur
