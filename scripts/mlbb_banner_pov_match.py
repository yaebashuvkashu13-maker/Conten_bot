#!/usr/bin/env python3
"""Match kill-banner hero portrait with POV skill-bar hero (reject spectator kills)."""

from __future__ import annotations

import os
from pathlib import Path


def _pov_match_enabled() -> bool:
    return os.environ.get("MLBB_BANNER_POV_MATCH", "1") == "1"


def _similarity_min() -> float:
    return float(os.environ.get("MLBB_BANNER_POV_MIN_SIM", "0.42"))


def extract_banner_hero_patch(frame) -> object | None:
    """Circular hero icon left of kill-streak banner text."""
    import cv2

    if frame is None:
        return None
    h, w = frame.shape[:2]
    if h < 80 or w < 160:
        return None
    y0, y1 = int(h * 0.03), int(h * 0.24)
    x0, x1 = int(w * 0.06), int(w * 0.22)
    patch = frame[y0:y1, x0:x1]
    if patch.size == 0:
        return None
    return cv2.resize(patch, (48, 48))


def _extract_patch_variants(frame, boxes: list[tuple[float, float, float, float]]) -> list[object]:
    import cv2

    if frame is None:
        return []
    h, w = frame.shape[:2]
    out = []
    for (y0r, y1r, x0r, x1r) in boxes:
        y0, y1 = int(h * y0r), int(h * y1r)
        x0, x1 = int(w * x0r), int(w * x1r)
        patch = frame[y0:y1, x0:x1]
        if patch.size == 0:
            continue
        out.append(cv2.resize(patch, (48, 48)))
    return out


def extract_pov_hero_patch(frame) -> object | None:
    """Player hero portrait in bottom-left skill bar."""
    import cv2

    if frame is None:
        return None
    h, w = frame.shape[:2]
    if h < 80 or w < 160:
        return None
    y0, y1 = int(h * 0.70), int(h * 0.94)
    x0, x1 = int(w * 0.015), int(w * 0.13)
    patch = frame[y0:y1, x0:x1]
    if patch.size == 0:
        return None
    return cv2.resize(patch, (48, 48))


def portrait_similarity(patch_a, patch_b) -> float:
    """0..1 histogram correlation in HSV (hue-robust for skin/portrait)."""
    import cv2
    import numpy as np

    if patch_a is None or patch_b is None:
        return 0.0
    try:
        a = cv2.cvtColor(patch_a, cv2.COLOR_BGR2HSV)
        b = cv2.cvtColor(patch_b, cv2.COLOR_BGR2HSV)
        hist_a = cv2.calcHist([a], [0, 1], None, [24, 16], [0, 180, 0, 256])
        hist_b = cv2.calcHist([b], [0, 1], None, [24, 16], [0, 180, 0, 256])
        cv2.normalize(hist_a, hist_a)
        cv2.normalize(hist_b, hist_b)
        corr = float(cv2.compareHist(hist_a, hist_b, cv2.HISTCMP_CORREL))
        return max(0.0, min(1.0, corr))
    except Exception:
        return 0.0


def banner_pov_hero_match(
    vod: Path,
    banner_sec: float,
    *,
    sample_offsets: tuple[float, ...] = (-0.7, -0.35, 0.0, 0.35, 0.7),
) -> tuple[bool, str, float]:
    """
    True when banner hero icon matches POV skill-bar hero at banner time.
  Spectator / teammate kill banners typically fail this check.
    """
    if not _pov_match_enabled():
        return True, "pov_match_off", 1.0

    from gameplay_gate import _read_frame_at

    best = 0.0
    for off in sample_offsets:
        frame = _read_frame_at(vod, max(0.0, float(banner_sec) + off))
        if frame is None:
            continue
        # Some MLBB layouts shift portraits; try a few nearby crops and take best similarity.
        banner_patches = _extract_patch_variants(
            frame,
            [
                (0.03, 0.24, 0.06, 0.22),
                (0.03, 0.26, 0.04, 0.20),
                (0.04, 0.26, 0.07, 0.23),
            ],
        )
        pov_patches = _extract_patch_variants(
            frame,
            [
                (0.70, 0.94, 0.015, 0.13),
                (0.68, 0.93, 0.010, 0.135),
                (0.72, 0.95, 0.020, 0.14),
            ],
        )
        for bp in banner_patches:
            for pp in pov_patches:
                best = max(best, portrait_similarity(bp, pp))

    need = _similarity_min()
    if best >= need:
        return True, f"pov_hero_ok sim={best:.3f}", best
    return False, f"pov_hero_mismatch sim={best:.3f} need>={need:.2f}", best


def banner_pov_hero_match_for_peak(
    vod: Path,
    peak_sec: float,
    *,
    banner_sec: float | None = None,
) -> tuple[bool, str, float]:
    """
    Try POV at explicit banner time, then re-scan OCR banner near peak.
    Highlight windows often sit a few seconds after the kill banner frame.
    """
    candidates: list[float] = []
    if banner_sec is not None:
        candidates.append(float(banner_sec))
    candidates.append(float(peak_sec))
    try:
        from mlbb_kill_banner import find_banner_near_peak

        hit = find_banner_near_peak(vod, float(peak_sec), quick=True)
        if hit is not None:
            candidates.append(float(hit.sec))
        if hit is None:
            hit = find_banner_near_peak(vod, float(peak_sec), quick=False)
            if hit is not None:
                candidates.append(float(hit.sec))
    except Exception:
        pass

    best_sim = 0.0
    best_reason = "pov_no_samples"
    for sec in sorted({round(c, 2) for c in candidates if c >= 0}):
        ok, reason, sim = banner_pov_hero_match(vod, sec)
        best_sim = max(best_sim, sim)
        if ok:
            return True, reason, sim
        best_reason = reason
    return False, best_reason, best_sim
