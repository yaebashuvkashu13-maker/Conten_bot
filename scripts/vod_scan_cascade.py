#!/usr/bin/env python3
"""Configurable multi-stage VOD scan funnel limits (DSP → ranker → PANNs → CLIP → payoff)."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class ScanCascadeLimits:
    dsp_candidates: int = 256
    fast_ranker: int = 50
    panns: int = 25
    clip_visual: int = 12
    kill_notification: int = 8
    montage_parts: int = 3

    def as_dict(self) -> dict[str, int]:
        return {
            "dsp_candidates": self.dsp_candidates,
            "fast_ranker": self.fast_ranker,
            "panns": self.panns,
            "clip_visual": self.clip_visual,
            "kill_notification": self.kill_notification,
            "montage_parts": self.montage_parts,
        }


def _int_env(name: str, default: int, *, low: int = 1, high: int = 512) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(low, min(high, value))


def cascade_limits() -> ScanCascadeLimits:
    """Effective cascade caps from environment (override per stage)."""
    return ScanCascadeLimits(
        dsp_candidates=_int_env("VOD_CASCADE_DSP_MAX", 256, low=48, high=512),
        fast_ranker=_int_env("VOD_CASCADE_FAST_RANKER_MAX", 50, low=20, high=128),
        panns=_int_env("VOD_CASCADE_PANN_MAX", 25, low=8, high=64),
        clip_visual=_int_env("VOD_CASCADE_CLIP_MAX", 12, low=4, high=32),
        kill_notification=_int_env("VOD_CASCADE_KILL_MAX", 8, low=3, high=24),
        montage_parts=_int_env("VOD_CASCADE_MONTAGE_PARTS", 3, low=2, high=5),
    )


def apply_cascade_to_pool(peaks: list[float], stage: str) -> list[float]:
    """Trim a ranked peak list to the configured stage cap."""
    limits = cascade_limits()
    cap = {
        "dsp": limits.dsp_candidates,
        "fast_ranker": limits.fast_ranker,
        "panns": limits.panns,
        "clip": limits.clip_visual,
        "kill": limits.kill_notification,
    }.get(stage, len(peaks))
    return list(peaks[:cap])


__all__ = ["ScanCascadeLimits", "apply_cascade_to_pool", "cascade_limits"]
