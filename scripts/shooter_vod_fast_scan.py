#!/usr/bin/env python3
"""Cheap PUBG/Standoff/WoT VOD preflight + dense gun-peak discovery for montages."""

from __future__ import annotations

import logging
import os
import json
import hashlib
import subprocess
from pathlib import Path

import numpy as np

from highlight_scorer import WINDOW_SEC, normalize_profile, score_panns_audio

log = logging.getLogger("shooter_vod_fast_scan")


def candidate_pool_target(min_clips: int = 2) -> int:
    """Keep enough ranked moments to survive strict presend false positives."""
    raw = os.environ.get("SHOOTER_VOD_CANDIDATE_POOL_TARGET", "16")
    return max(10, int(raw), int(min_clips) * 4)


def _audio_candidate_cache_file(video_path: Path) -> Path:
    stat = video_path.stat()
    raw = f"v1|{video_path.resolve()}|{stat.st_size}|{stat.st_mtime_ns}"
    key = hashlib.sha256(raw.encode()).hexdigest()[:32]
    root = Path(
        os.environ.get(
            "PUBG_AUDIO_CANDIDATE_CACHE",
            "/root/data/pubg/audio_candidate_cache",
        )
    )
    return root / f"{key}.json"


def _rank_audio_windows(
    pcm: np.ndarray,
    *,
    sample_rate: int,
    base_sec: float,
    window_sec: float = 4.0,
    step_sec: float = 2.0,
    max_candidates: int = 96,
    gap_sec: float = 10.0,
) -> list[float]:
    """Rank gun-transient windows from one decoded PCM stream."""
    if pcm.size < sample_rate * window_sec:
        return []
    samples = pcm.astype(np.float32) / 32768.0
    frame = 256
    usable = (len(samples) // frame) * frame
    energies = np.sqrt(np.mean(samples[:usable].reshape(-1, frame) ** 2, axis=1))
    frames_per_window = max(8, int(window_sec * sample_rate / frame))
    frames_per_step = max(1, int(step_sec * sample_rate / frame))
    scored: list[tuple[float, float]] = []
    for offset in range(0, max(0, len(energies) - frames_per_window), frames_per_step):
        values = energies[offset : offset + frames_per_window]
        median = float(np.median(values))
        mean = float(np.mean(values))
        peak = float(np.max(values))
        floor = max(median * 2.6, 0.010)
        spikes = np.count_nonzero(
            (values[1:] > floor) & (values[1:] > values[:-1] * 1.55)
        )
        density = float(spikes) / max(len(values) - 1, 1)
        burst = peak / max(mean, 1e-6)
        score = density * 4.0 + min(12.0, burst) * 0.012 + min(0.10, mean)
        if density >= 0.012 or (burst >= 4.0 and peak >= 0.025):
            center = base_sec + (offset * frame / sample_rate) + window_sec * 0.5
            scored.append((score, center))
    scored.sort(key=lambda row: -row[0])
    picked: list[float] = []
    for _score, center in scored:
        if any(abs(center - old) < gap_sec for old in picked):
            continue
        picked.append(round(center, 1))
        if len(picked) >= max_candidates:
            break
    return picked


def discover_audio_candidate_offsets(
    video_path: Path,
    *,
    duration: float,
    skip_intro: float,
) -> list[float]:
    """Decode audio once and return high-recall transient candidates."""
    cache_file = _audio_candidate_cache_file(video_path)
    try:
        cached = json.loads(cache_file.read_text(encoding="utf-8"))
        if cached.get("version") == 1:
            return [float(value) for value in cached.get("peaks") or []]
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        pass

    sample_rate = 11025
    scan_duration = max(0.0, float(duration) - float(skip_intro) - 5.0)
    command = [
        "ffmpeg",
        "-v",
        "error",
        "-hwaccel",
        "none",
        "-ss",
        f"{skip_intro:.3f}",
        "-t",
        f"{scan_duration:.3f}",
        "-i",
        str(video_path),
        "-vn",
        "-ac",
        "1",
        "-ar",
        str(sample_rate),
        "-f",
        "s16le",
        "-",
    ]
    try:
        proc = subprocess.run(command, capture_output=True, timeout=900, check=False)
    except subprocess.TimeoutExpired:
        return []
    if proc.returncode != 0 or not proc.stdout:
        return []
    pcm = np.frombuffer(proc.stdout, dtype=np.int16)
    peaks = _rank_audio_windows(
        pcm,
        sample_rate=sample_rate,
        base_sec=skip_intro,
        max_candidates=max(
            candidate_pool_target() * 3,
            int(os.environ.get("SHOOTER_VOD_AUDIO_CANDIDATE_MAX", "96")),
        ),
        gap_sec=float(os.environ.get("SHOOTER_VOD_AUDIO_CANDIDATE_GAP_SEC", "10")),
    )
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = cache_file.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps({"version": 1, "peaks": peaks}), encoding="utf-8")
    os.replace(tmp, cache_file)
    return peaks


def _skip_intro_sec(profile: str, *, duration: float | None = None) -> float:
    profile = normalize_profile(profile)
    if profile == "pubg":
        skip = float(
            os.environ.get(
                "PUBG_METRO_VOD_SKIP_INTRO_SEC",
                os.environ.get("SHOOTER_VOD_FAST_SKIP_INTRO", "120"),
            )
        )
    else:
        # Standoff/WoT intros are shorter — Metro 120s skip wastes early fights.
        skip = float(os.environ.get("SHOOTER_VOD_FAST_SKIP_INTRO", "60"))
    # Short combat VODs (~2–4 min) must still get probes; fixed 60–120s skip
    # left only 1 empty window and exhausted the inbox.
    if duration is not None and duration > 0:
        skip = min(skip, max(12.0, float(duration) * 0.12))
    return skip


def _probe_offsets(duration: float, *, skip_intro: float) -> list[float]:
    dur = max(0.0, float(duration))
    # Allow short fight clips (≥90s usable body).
    if dur < skip_intro + 45:
        return []
    offsets: list[float] = []
    for delta in (0, 45, 90, 150, 360, 720, 1200, 1800):
        t = skip_intro + delta
        if t + WINDOW_SEC < dur - 8:
            offsets.append(round(t, 1))
    mid = skip_intro + max(0.0, (dur - skip_intro) * 0.42)
    if mid + WINDOW_SEC < dur - 8 and all(abs(mid - x) > 35 for x in offsets):
        offsets.append(round(mid, 1))
    return sorted(set(offsets))[: int(os.environ.get("SHOOTER_VOD_FAST_PROBE_MAX", "6"))]


def _dense_offsets(duration: float, *, skip_intro: float, probe_pass: int = 0) -> list[float]:
    """Evenly spaced probes for montage (≥3 fights). Caps CPU via MAX.

    Critical: a fixed step+cap only covered the first ~20min of 90min streams,
    so used early fights left have<3 while the rest of the VOD was never probed.
    probe_pass shifts the grid so later visits scan different timeline slices.
    """
    dur = max(0.0, float(duration))
    # Short VODs: denser step, still probe (was empty at skip+180 threshold).
    if dur < skip_intro + 120:
        return _probe_offsets(dur, skip_intro=skip_intro)
    step = float(os.environ.get("SHOOTER_VOD_DENSE_PROBE_STEP_SEC", "40"))
    if dur < 600:
        step = min(step, 25.0)
    # A 2-part montage used to cap the ranked pool at 8 moments. Keep at least
    # three coarse probes per desired candidate so a 10+ moment pool is possible.
    cap = max(
        10,
        candidate_pool_target() * 3,
        int(os.environ.get("SHOOTER_VOD_DENSE_PROBE_MAX", "32")),
    )
    usable = max(0.0, dur - skip_intro - 12.0 - WINDOW_SEC)
    if usable > 0 and cap > 1:
        # Stretch so probes span the whole VOD instead of clustering at the start.
        step = max(step, usable / float(cap - 1))
    phase_shift = 0.0
    if probe_pass > 0 and usable > 0:
        # Irrational phase avoids pass 2 wrapping onto pass 0. The old 2.5×
        # formula generated only two unique grids, regardless of configured passes.
        phase_shift = (probe_pass * step * 0.38196601125) % step
    out: list[float] = []
    t = skip_intro + phase_shift
    while t + WINDOW_SEC < dur - 12 and len(out) < cap:
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

    skip = _skip_intro_sec(profile, duration=dur)
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
    lo = max(8.0, float(approx_center) - float(search_radius))
    hi = min(float(duration) - 8.0, float(approx_center) + float(search_radius))
    t = lo
    while t <= hi + 1e-6:
        a = max(0.0, t - sample_sec * 0.5)
        try:
            gun, _burst, _rms = score_pubg_gunfire_audio(video_path, a, sample_sec)
        except Exception:
            gun = 0.0
        if float(gun) > best_gun + 1e-9:
            best_gun = float(gun)
            best_c = float(t)
        t += float(step)
    # Coarse dense scan already supplied a PANN score. Confirm only the selected
    # local gun maximum instead of running the neural model at every 3s offset.
    try:
        best_start = max(0.0, best_c - sample_sec * 0.5)
        panns = score_panns_audio(video_path, best_start, sample_sec)
        best_panns = float(panns.get("panns_gun_max", 0) or 0)
    except Exception:
        best_panns = 0.0
    return round(best_c, 1), max(0.0, best_gun), best_panns


def discover_montage_gun_peaks(
    video_path: Path,
    profile: str,
    *,
    min_clips: int = 3,
    gap_sec: float = 55.0,
    probe_pass: int = 0,
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

    skip = _skip_intro_sec(profile, duration=dur)
    gun_min = float(os.environ.get("SHOOTER_VOD_DENSE_PANN_MIN", "0.16"))
    dens_min = float(os.environ.get("SHOOTER_VOD_DENSE_GUN_MIN", "0.045"))
    audio_generator = (
        profile == "pubg" and os.environ.get("SHOOTER_VOD_AUDIO_GENERATOR", "1") == "1"
    )
    if audio_generator:
        audio_centers = discover_audio_candidate_offsets(
            video_path,
            duration=dur,
            skip_intro=skip,
        )
        offsets = [max(0.0, center - WINDOW_SEC * 0.5) for center in audio_centers]
    else:
        offsets = _dense_offsets(dur, skip_intro=skip, probe_pass=probe_pass)
    if not offsets:
        return [], "dense_probe_too_short"

    try:
        from panns_audio_cache import prewarm_grid

        prewarm_grid(video_path, offsets, WINDOW_SEC)
    except Exception:
        pass

    log.info(
        "dense gun probe start vod=%s offsets=%s skip=%.0f pass=%s",
        video_path.name,
        len(offsets),
        skip,
        probe_pass,
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
    pool_cap = candidate_pool_target(min_clips)
    # First pass: space by PANNs only (cheap).
    shortlist: list[tuple[float, float]] = []  # panns, center
    for panns_g, center in scored:
        if any(abs(center - c) < gap_sec for _g, c in shortlist):
            continue
        shortlist.append((panns_g, center))
        if len(shortlist) >= pool_cap:
            break

    # Fill the recall pool, not merely the 2-part montage minimum. With 30s
    # probes and a 55s first-pass gap, 15 real hits collapsed to 8–9 candidates.
    desired = min(pool_cap, len(scored))
    if len(shortlist) < desired and gap_sec > 25:
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
    pick_gap = gap_sec * 0.85
    for center, gun_d, panns_g in snapped:
        if any(abs(center - p) < pick_gap for p in picked):
            continue
        picked.append(center)
        picked_scores.append(float(gun_d) * 2.0 + float(panns_g))
        if len(picked) >= pool_cap:
            break

    # Owner ×3 склейка: if spacing ate the third fight, tighten once more.
    if len(picked) < min_clips and snapped:
        ultra = max(14.0, gap_sec * 0.35)
        if ultra < pick_gap:
            picked = []
            picked_scores = []
            for center, gun_d, panns_g in snapped:
                if any(abs(center - p) < ultra for p in picked):
                    continue
                picked.append(center)
                picked_scores.append(float(gun_d) * 2.0 + float(panns_g))
                if len(picked) >= pool_cap:
                    break
            pick_gap = ultra
            gap_sec = ultra / 0.85 if ultra > 0 else gap_sec

    # Keep strength order (not chronological) so montage tries best fights first.
    # Chronological reorder happens only after parts are accepted for xfade.
    top = scored[0][0] if scored else 0.0
    reason = (
        f"{'audio_generator' if audio_generator else 'dense_panns'} "
        f"hits={len(scored)}/{len(offsets)} shortlist={len(shortlist)} "
        f"snapped={len(snapped)} picked={len(picked)} gap={pick_gap:.0f} top={top:.3f} pass={probe_pass}"
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
