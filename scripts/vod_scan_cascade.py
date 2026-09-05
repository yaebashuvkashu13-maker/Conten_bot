#!/usr/bin/env python3
"""Configurable multi-stage VOD scan funnel limits (DSP → ranker → PANNs → CLIP → payoff).

Full-scan mode (default for PUBG drought recovery):
  PUBG_FULL_PEAK_SCAN=1  → do not truncate peak pools between stages
  Stage env caps use 0 to mean unlimited.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class ScanCascadeLimits:
    dsp_candidates: int = 0  # 0 = unlimited
    fast_ranker: int = 0
    panns: int = 0
    clip_visual: int = 0
    kill_notification: int = 0
    montage_parts: int = 3

    def as_dict(self) -> dict[str, int]:
        return {
            "dsp_candidates": self.dsp_candidates,
            "fast_ranker": self.fast_ranker,
            "panns": self.panns,
            "clip_visual": self.clip_visual,
            "kill_notification": self.kill_notification,
            "montage_parts": self.montage_parts,
            "full_peak_scan": int(full_peak_scan_enabled()),
        }


def full_peak_scan_enabled() -> bool:
    """When on, cascade stages keep the full peak list (no top-N truncation)."""
    return os.environ.get("PUBG_FULL_PEAK_SCAN", "1") == "1"


def _int_env(name: str, default: int, *, low: int = 0, high: int = 100_000) -> int:
    """Parse int env; 0 means unlimited (caller must honor)."""
    try:
        value = int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        value = default
    if value <= 0:
        return 0
    return max(low if low > 0 else 1, min(high, value))


def cascade_limits() -> ScanCascadeLimits:
    """Effective cascade caps from environment (override per stage).

    Defaults are unlimited (0) under full-peak scan so long VODs are not
    collapsed to an arbitrary top-8 kill shortlist. Legacy tight caps remain
    available by setting PUBG_FULL_PEAK_SCAN=0 and the VOD_CASCADE_* envs.
    """
    if full_peak_scan_enabled():
        # Full scan: ignore stale VOD_CASCADE_* caps in env (they used to silently
        # re-impose PANN=25 / CLIP=12). Only montage_parts still bounds ship size.
        return ScanCascadeLimits(
            dsp_candidates=0,
            fast_ranker=0,
            panns=0,
            clip_visual=0,
            kill_notification=0,
            montage_parts=_int_env("VOD_CASCADE_MONTAGE_PARTS", 3, low=1, high=12),
        )
    # Legacy / explicit tight funnel (opt-in via PUBG_FULL_PEAK_SCAN=0).
    return ScanCascadeLimits(
        dsp_candidates=_int_env("VOD_CASCADE_DSP_MAX", 256, low=48, high=2000),
        fast_ranker=_int_env("VOD_CASCADE_FAST_RANKER_MAX", 50, low=20, high=2000),
        panns=_int_env("VOD_CASCADE_PANN_MAX", 25, low=8, high=500),
        clip_visual=_int_env("VOD_CASCADE_CLIP_MAX", 12, low=4, high=200),
        kill_notification=_int_env("VOD_CASCADE_KILL_MAX", 8, low=3, high=500),
        montage_parts=_int_env("VOD_CASCADE_MONTAGE_PARTS", 3, low=2, high=12),
    )


def apply_cascade_to_pool(peaks: list[float], stage: str) -> list[float]:
    """Optionally trim a ranked peak list to the configured stage cap.

    Cap 0 / full-peak scan → return the full list (no silent top-8 drop).
    """
    if full_peak_scan_enabled():
        return list(peaks)
    limits = cascade_limits()
    cap = {
        "dsp": limits.dsp_candidates,
        "fast_ranker": limits.fast_ranker,
        "panns": limits.panns,
        "clip": limits.clip_visual,
        "kill": limits.kill_notification,
    }.get(stage, 0)
    if cap <= 0:
        return list(peaks)
    return list(peaks[:cap])


__all__ = [
    "ScanCascadeLimits",
    "apply_cascade_to_pool",
    "cascade_limits",
    "full_peak_scan_enabled",
]
