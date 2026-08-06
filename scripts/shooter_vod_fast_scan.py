#!/usr/bin/env python3
"""Cheap PUBG/Standoff/WoT VOD preflight + dense gun-peak discovery for montages."""

from __future__ import annotations

import logging
import os
from pathlib import Path

from highlight_scorer import WINDOW_SEC, normalize_profile, score_panns_audio

log = logging.getLogger("shooter_vod_fast_scan")


def _skip_intro_sec(profile: str) -> float:
    profile = normalize_profile(profile)
    if profile == "pubg":
        return float(
            os.environ.get(
                "PUBG_METRO_VOD_SKIP_INTRO_SEC",
                os.environ.get("SHOOTER_VOD_FAST_SKIP_INTRO", "120"),
            )
        )
    # Standoff/WoT intros are shorter — Metro 120s skip wastes early fights.
    return float(os.environ.get("SHOOTER_VOD_FAST_SKIP_INTRO", "60"))


def _probe_offsets(duration: float, *, skip_intro: float) -> list[float]:
    dur = max(0.0, float(duration))
    if dur < skip_intro + 90:
        return []
    offsets: list[float] = []
    for delta in (0, 150, 360, 720, 1200, 1800):
        t = skip_intro + delta
        if t + WINDOW_SEC < dur - 45:
            offsets.append(round(t, 1))
    mid = skip_intro + max(0.0, (dur - skip_intro) * 0.42)
    if mid + WINDOW_SEC < dur - 45 and all(abs(mid - x) > 90 for x in offsets):
        offsets.append(round(mid, 1))
    return sorted(set(offsets))[: int(os.environ.get("SHOOTER_VOD_FAST_PROBE_MAX", "6"))]


def _dense_offsets(duration: float, *, skip_intro: float) -> list[float]:
    """Evenly spaced probes for montage (≥3 fights). Caps CPU via MAX."""
    dur = max(0.0, float(duration))
    if dur < skip_intro + 180:
        return _probe_offsets(dur, skip_intro=skip_intro)
    step = float(os.environ.get("SHOOTER_VOD_DENSE_PROBE_STEP_SEC", "40"))
    cap = max(10, int(os.environ.get("SHOOTER_VOD_DENSE_PROBE_MAX", "32")))
    out: list[float] = []
    t = skip_intro
    while t + WINDOW_SEC < dur - 40 and len(out) < cap:
        out.append(round(t, 1))
        t += step
    return out


def vod_fast_combat_check(
    video_path: Path,
    profile: str,
) -> tuple[bool, str, list[float]]:
    """
    Sparse PANNs probe (4–6 windows). Returns (ok, reason, gun_peak_starts).
    ~1–3 min vs 15–30 min full highlight on CPU.
    """
    profile = normalize_profile(profile)
    if os.environ.get("SHOOTER_VOD_FAST_PROBE", "1") != "1":
        return True, "fast_probe_disabled", []

    from smart_video_editor import ffprobe_duration

    dur = ffprobe_duration(video_path)
    if dur <= 0:
        return False, "fast_probe_no_duration", []

    skip = _skip_intro_sec(profile)
    offsets = _probe_offsets(dur, skip_intro=skip)
    if not offsets:
        return False, "fast_probe_too_short", []

    gun_min = float(os.environ.get("SHOOTER_VOD_FAST_PANN_MIN", "0.14"))
    hits: list[float] = []
    top_gun = 0.0
    for t in offsets:
        panns = score_panns_audio(video_path, t, WINDOW_SEC)
        gmax = float(panns.get("panns_gun_max", 0))
        top_gun = max(top_gun, gmax)
        if gmax >= gun_min:
            # Seed as window center — highlight/montage expect peak centers.
            hits.append(round(t + WINDOW_SEC * 0.5, 1))

    if not hits:
        return (
            False,
            f"fast_panns_0/{len(offsets)} top={top_gun:.3f} min={gun_min:.2f}",
            [],
        )
    return True, f"fast_panns_{len(hits)}/{len(offsets)} top={top_gun:.3f}", hits


def snap_peak_to_gunfire(
    video_path: Path,
    approx_center: float,
    *,
    duration: float,
    search_radius: float = 10.0,
    step: float = 3.0,
    sample_sec: float = 4.0,
) -> tuple[float, float, float]:
    """
    Re-center a probe on the loudest local gunfire using the same density metric
    as the shooting gate. Returns (center, gun_density, panns_gun_max).

    Kept cheap: ~7 audio windows, not dozens.
    """
    from gameplay_gate import score_pubg_gunfire_audio

    best_c = float(approx_center)
    best_gun = -1.0
    best_panns = 0.0
    lo = max(8.0, float(approx_center) - float(search_radius))
    hi = min(float(duration) - 8.0, float(approx_center) + float(search_radius))
    t = lo
    while t <= hi + 1e-6:
        a = max(0.0, t - sample_sec * 0.5)
        try:
            gun, _burst, _rms = score_pubg_gunfire_audio(video_path, a, sample_sec)
        except Exception:
            gun = 0.0
        try:
            panns = score_panns_audio(video_path, a, sample_sec)
            pmax = float(panns.get("panns_gun_max", 0) or 0)
        except Exception:
            pmax = 0.0
        score = float(gun) * 2.0 + pmax
        if score > best_gun * 2.0 + best_panns + 1e-9:
            best_gun = float(gun)
            best_panns = pmax
            best_c = float(t)
        t += float(step)
    return round(best_c, 1), max(0.0, best_gun), best_panns


def discover_montage_gun_peaks(
    video_path: Path,
    profile: str,
    *,
    min_clips: int = 3,
    gap_sec: float = 55.0,
) -> tuple[list[float], str]:
    """
    Dense PANNs scan → spaced candidates → snap only those → ×3 montage peaks.

    Snap is applied ONLY to the shortlist (not every probe) so we stay minutes,
    not half an hour of ffmpeg audio extracts.
    """
    profile = normalize_profile(profile)
    from smart_video_editor import ffprobe_duration

    dur = ffprobe_duration(video_path)
    if dur <= 0:
        return [], "dense_probe_no_duration"

    skip = _skip_intro_sec(profile)
    gun_min = float(os.environ.get("SHOOTER_VOD_DENSE_PANN_MIN", "0.16"))
    dens_min = float(os.environ.get("SHOOTER_VOD_DENSE_GUN_MIN", "0.045"))
    offsets = _dense_offsets(dur, skip_intro=skip)
    if not offsets:
        return [], "dense_probe_too_short"

    log.info(
        "dense gun probe start vod=%s offsets=%s skip=%.0f",
        video_path.name,
        len(offsets),
        skip,
    )
    scored: list[tuple[float, float]] = []  # panns, center_hint
    for t in offsets:
        panns = score_panns_audio(video_path, t, WINDOW_SEC)
        gmax = float(panns.get("panns_gun_max", 0))
        if gmax >= gun_min:
            scored.append((gmax, t + WINDOW_SEC * 0.5))

    if len(scored) < min_clips:
        return [], f"dense_panns_0/{len(offsets)}" if not scored else (
            f"dense_panns hits={len(scored)}/{len(offsets)} picked=0"
        )

    scored.sort(key=lambda x: -x[0])
    pool_cap = max(min_clips * 4, min_clips + 6)
    # First pass: space by PANNs only (cheap).
    shortlist: list[tuple[float, float]] = []  # panns, center
    for panns_g, center in scored:
        if any(abs(center - c) < gap_sec for _g, c in shortlist):
            continue
        shortlist.append((panns_g, center))
        if len(shortlist) >= pool_cap:
            break

    if len(shortlist) < min_clips and gap_sec > 25:
        tight = max(22.0, gap_sec * 0.45)
        shortlist = []
        for panns_g, center in scored:
            if any(abs(center - c) < tight for _g, c in shortlist):
                continue
            shortlist.append((panns_g, center))
            if len(shortlist) >= pool_cap:
                break
        gap_sec = tight

    # Snap only the shortlist onto local gunfire (gate metric).
    snapped: list[tuple[float, float, float]] = []  # center, gun, panns
    for panns_g, center in shortlist:
        c2, gun_d, pmax = snap_peak_to_gunfire(video_path, center, duration=dur)
        panns_use = max(panns_g, pmax)
        if gun_d < dens_min and panns_use < gun_min * 1.15:
            log.info(
                "dense snap drop center=%.0f→%.0f gun=%.3f panns=%.3f",
                center,
                c2,
                gun_d,
                panns_use,
            )
            continue
        snapped.append((c2, gun_d, panns_use))

    snapped.sort(key=lambda x: -(x[1] * 2.0 + x[2]))
    picked: list[float] = []
    picked_scores: list[float] = []
    for center, gun_d, panns_g in snapped:
        if any(abs(center - p) < gap_sec * 0.85 for p in picked):
            continue
        picked.append(center)
        picked_scores.append(float(gun_d) * 2.0 + float(panns_g))
        if len(picked) >= pool_cap:
            break

    # Keep strength order (not chronological) so montage tries best fights first.
    # Chronological reorder happens only after parts are accepted for xfade.
    top = scored[0][0] if scored else 0.0
    reason = (
        f"dense_panns hits={len(scored)}/{len(offsets)} shortlist={len(shortlist)} "
        f"snapped={len(snapped)} picked={len(picked)} gap={gap_sec:.0f} top={top:.3f}"
    )
    log.info("dense gun peaks vod=%s %s peaks=%s", video_path.name, reason, picked[:8])
    return picked, reason


def apply_fast_probe_seeds(peaks: list[float]) -> None:
    if not peaks or os.environ.get("SHOOTER_VOD_SEED_FROM_FAST_PROBE", "1") != "1":
        return
    os.environ["HIGHLIGHT_ALLOW_SEED_STARTS"] = "1"
    os.environ["HIGHLIGHT_SEED_STARTS"] = ",".join(str(round(p, 1)) for p in peaks[:8])


def clear_fast_probe_seeds() -> None:
    os.environ.pop("HIGHLIGHT_SEED_STARTS", None)
    os.environ.pop("HIGHLIGHT_ALLOW_SEED_STARTS", None)
