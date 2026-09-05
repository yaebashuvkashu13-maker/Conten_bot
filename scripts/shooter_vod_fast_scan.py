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
AUDIO_GENERATOR_VERSION = 4


def dense_pcm_max_sec(duration: float | None = None) -> float:
    """PCM extract budget — prefers full-VOD coverage; soft safety cap only.

    Old default 4200s quietly dropped fights after ~70min. Duration-aware budget
    keeps the tail in play; set SHOOTER_VOD_DENSE_PCM_MAX_SEC>0 to force a hard cap.
    """
    forced = float(os.environ.get("SHOOTER_VOD_DENSE_PCM_MAX_SEC", "0") or 0)
    if forced > 0:
        return max(600.0, forced)
    if duration is not None and duration > 0:
        try:
            from pubg_combat_timeline import dense_scan_span_for_duration

            return max(600.0, dense_scan_span_for_duration(float(duration), 0.0))
        except Exception:
            return max(600.0, float(duration))
    # No duration known yet — generous default (3h) instead of 70min.
    return max(600.0, float(os.environ.get("SHOOTER_VOD_DENSE_PCM_FALLBACK_SEC", "10800")))


def dense_scan_span(duration: float, skip: float) -> float:
    raw = max(0.0, float(duration) - float(skip) - 12.0)
    return min(raw, dense_pcm_max_sec(duration))


def candidate_pool_target(min_clips: int = 2, duration: float | None = None) -> int:
    """Keep enough ranked moments to survive strict presend false positives.

    Scales with VOD length so end-of-VOD fights are not truncated by a fixed top-N.
    Under PUBG_FULL_PEAK_SCAN the default target is large so the pool is not
    silently collapsed to ~16 before kill/rank stages.
    """
    full_scan = os.environ.get("PUBG_FULL_PEAK_SCAN", "1") == "1"
    default_target = "256" if full_scan else "16"
    raw = os.environ.get("SHOOTER_VOD_CANDIDATE_POOL_TARGET", default_target)
    base = max(10, int(raw), int(min_clips) * 4)
    if duration is not None and duration > 0:
        try:
            from pubg_combat_timeline import adaptive_candidate_pool

            return max(base, adaptive_candidate_pool(float(duration), min_clips=min_clips))
        except Exception:
            # ~1 candidate / 90s body as a local fallback.
            return max(base, int(float(duration) / 90.0) + 6)
    return base


def _audio_candidate_cache_file(video_path: Path) -> Path:
    stat = video_path.stat()
    raw = (
        f"v{AUDIO_GENERATOR_VERSION}|{video_path.resolve()}|{stat.st_size}|{stat.st_mtime_ns}|"
        f"{os.environ.get('SHOOTER_VOD_AUDIO_CANDIDATE_MAX', '96')}|"
        f"{os.environ.get('SHOOTER_VOD_AUDIO_CANDIDATE_GAP_SEC', '10')}"
    )
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
    chunk_sec: float = 300.0,
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
    chunks: dict[int, list[tuple[float, float]]] = {}
    for row in scored:
        chunks.setdefault(int(row[1] // max(chunk_sec, 1.0)), []).append(row)
    quota = max(1, min(4, max_candidates // max(len(chunks), 1)))
    selected: list[tuple[float, float]] = []
    selected_centers: list[float] = []
    for chunk_rows in chunks.values():
        used = 0
        for row in chunk_rows:
            if any(abs(row[1] - old) < gap_sec for old in selected_centers):
                continue
            selected.append(row)
            selected_centers.append(row[1])
            used += 1
            if used >= quota:
                break
    for row in scored:
        if len(selected) >= max_candidates:
            break
        if any(abs(row[1] - old) < gap_sec for old in selected_centers):
            continue
        selected.append(row)
        selected_centers.append(row[1])
    selected.sort(key=lambda row: -row[0])
    picked = [round(center, 1) for _score, center in selected[:max_candidates]]
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
        if cached.get("version") == AUDIO_GENERATOR_VERSION:
            return [float(value) for value in cached.get("peaks") or []]
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        pass

    sample_rate = 11025
    scan_duration = max(0.0, float(duration) - float(skip_intro) - 5.0)
    pcm = None
    try:
        from vod_feature_store import open_store

        store = open_store(video_path, skip_intro=skip_intro)
        if store is not None and store.ensure_pcm(scan_duration):
            pcm = store.get_pcm_s16()
            store.close()
    except Exception:
        pcm = None
    if pcm is None or pcm.size == 0:
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
    tmp.write_text(
        json.dumps({"version": AUDIO_GENERATOR_VERSION, "peaks": peaks}),
        encoding="utf-8",
    )
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
    # Probe count scales with VOD length — fixed caps previously clustered on the
    # first ~20–70min and starved fights near the end of long streams.
    cap = max(
        10,
        candidate_pool_target(duration=dur) * 3,
        int(os.environ.get("SHOOTER_VOD_DENSE_PROBE_MAX", "32")),
        int(max(0.0, dur - skip_intro) / 45.0) + 8,
    )
    # Soft safety only (not a quality "top-N scenes" limit).
    cap = min(cap, int(os.environ.get("SHOOTER_VOD_DENSE_PROBE_HARD_MAX", "240")))
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
    confirm_panns: bool = True,
    pcm_cache: object | None = None,
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
            if pcm_cache is not None:
                gun, _burst, _rms = pcm_cache.gunfire_metrics(a, sample_sec)
            else:
                gun, _burst, _rms = score_pubg_gunfire_audio(video_path, a, sample_sec)
        except Exception:
            gun = 0.0
        if float(gun) > best_gun + 1e-9:
            best_gun = float(gun)
            best_c = float(t)
        t += float(step)
    # Coarse dense scan already supplied a PANN score. Confirm only the selected
    # local gun maximum instead of running the neural model at every 3s offset.
    best_panns = 0.0
    if confirm_panns:
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
    funnel: object | None = None,
) -> tuple[list[float], str]:
    """
    Dense scan → spaced candidates → snap only those → montage peaks.

    With SHOOTER_VOD_AUDIO_BATCH=1: one PCM extract, DSP on all offsets,
    PANNs only on top SHOOTER_VOD_PANN_TOP_N windows (default 40).
    """
    profile = normalize_profile(profile)
    from smart_video_editor import ffprobe_duration

    try:
        from vod_peak_feature_cache import cache_enabled, get_cached

        if cache_enabled():
            hit = get_cached(video_path, probe_pass)
            if hit and len(hit.get("peaks") or []) >= min_clips:
                peaks = [float(p) for p in hit["peaks"]]
                reason = str(hit.get("reason") or f"feature_cache_{len(peaks)}")
                if funnel is not None:
                    funnel.feature_cache_hit = True
                    funnel.picked = len(peaks)
                    cached_funnel = hit.get("funnel")
                    if isinstance(cached_funnel, dict):
                        funnel.dsp_pass = int(cached_funnel.get("dsp_pass") or 0)
                        funnel.panns_pass = int(cached_funnel.get("panns_pass") or 0)
                log.info("dense gun peaks cache hit vod=%s n=%s", video_path.name, len(peaks))
                return peaks, f"feature_cache {reason}"
    except Exception:
        pass

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

    scan_end = skip + dense_scan_span(dur, skip)
    offsets = [t for t in offsets if float(t) <= max(skip, scan_end - WINDOW_SEC)]
    if not offsets:
        return [], "dense_probe_too_short"

    if funnel is not None:
        funnel.offsets_probed = len(offsets)

    pcm_cache = None
    batch_stats: dict[str, float | int] = {}
    scored: list[tuple[float, float]] = []
    if audio_generator:
        try:
            from vod_audio_batch import VodPcmCache, extract_vod_pcm_s16, pcm_to_float

            pcm = extract_vod_pcm_s16(video_path, skip, dense_scan_span(dur, skip))
            pcm_float = pcm_to_float(pcm)
            if pcm_float.size > 0:
                pcm_cache = VodPcmCache(pcm_float, base_sec=skip)
        except Exception:
            pcm_cache = None
        for index, t in enumerate(offsets):
            center = float(t) + WINDOW_SEC * 0.5
            gun_d = dens_min
            if pcm_cache is not None:
                try:
                    gun_d, _, _ = pcm_cache.gunfire_metrics(max(0.0, center - 2.0), 4.0)
                except Exception:
                    gun_d = dens_min
            rank = float(gun_d) + (1.0 - index / max(len(offsets), 1)) * 0.01
            scored.append((rank, center))
        scored.sort(key=lambda item: -item[0])
        # Full peak scan: PANNs-enrich the whole DSP shortlist, not a silent top-25.
        default_pann_top = "0" if os.environ.get("PUBG_FULL_PEAK_SCAN", "1") == "1" else "25"
        panns_cap = int(os.environ.get("PUBG_AUDIO_GEN_PANN_TOP", default_pann_top) or 0)
        if panns_cap <= 0:
            panns_cap = len(scored)
        if panns_cap > 0 and scored:
            enriched: list[tuple[float, float]] = []
            for rank, center in scored[:panns_cap]:
                pmax = 0.0
                try:
                    panns = score_panns_audio(video_path, max(0.0, center - 7.0), WINDOW_SEC)
                    pmax = float(panns.get("panns_gun_max", 0) or 0.0)
                except Exception:
                    pmax = 0.0
                enriched.append((max(rank, pmax * 0.35), center))
            scored = enriched + scored[panns_cap:]
        if funnel is not None:
            funnel.dsp_pass = len(scored)
    else:
        pcm_float = None
        try:
            from vod_audio_batch import (
                VodPcmCache,
                batch_enabled,
                discover_scored_windows,
                extract_vod_pcm_s16,
                pcm_to_float,
            )

            if batch_enabled():
                pcm = extract_vod_pcm_s16(video_path, skip, dense_scan_span(dur, skip))
                pcm_float = pcm_to_float(pcm)
                if pcm_float.size > 0:
                    pcm_cache = VodPcmCache(pcm_float, base_sec=skip)
                scored, batch_stats = discover_scored_windows(
                    video_path,
                    offsets,
                    WINDOW_SEC,
                    gun_min=gun_min,
                    pcm_float=pcm_float if pcm_float.size > 0 else None,
                    pcm_base_sec=skip,
                )
                if funnel is not None:
                    funnel.merge_timings(batch_stats)
                    funnel.panns_pass = len(scored)
        except Exception:
            log.exception("batch audio discovery failed vod=%s — fallback sequential", video_path.name)
            scored = []

        if not scored:
            try:
                from panns_audio_cache import prewarm_grid

                prewarm_grid(video_path, offsets, WINDOW_SEC)
            except Exception:
                pass
            for t in offsets:
                panns = score_panns_audio(video_path, t, WINDOW_SEC)
                gmax = float(panns.get("panns_gun_max", 0))
                if gmax >= gun_min:
                    scored.append((gmax, t + WINDOW_SEC * 0.5))
            if funnel is not None:
                funnel.panns_pass = len(scored)

    log.info(
        "dense gun probe start vod=%s offsets=%s skip=%.0f pass=%s",
        video_path.name,
        len(offsets),
        skip,
        probe_pass,
    )

    if len(scored) < min_clips:
        return [], f"dense_panns_0/{len(offsets)}" if not scored else (
            f"dense_panns hits={len(scored)}/{len(offsets)} picked=0"
        )

    scored.sort(key=lambda x: -x[0])
    try:
        from vod_scan_cascade import cascade_limits, full_peak_scan_enabled

        limits = cascade_limits()
        # Cap 0 / full-peak scan must NOT slice to scored[:0] (empty pool).
        if not full_peak_scan_enabled() and limits.fast_ranker > 0:
            scored = scored[: limits.fast_ranker]
        if funnel is not None:
            funnel.fast_ranker_pass = len(scored)
            funnel.note_stage("fast_ranker", len(scored))
    except Exception:
        pass
    pool_cap = candidate_pool_target(min_clips, duration=dur)
    shortlist: list[tuple[float, float]] = []
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

    if funnel is not None:
        funnel.shortlist = len(shortlist)

    snapped: list[tuple[float, float, float]] = []
    for panns_g, center in shortlist:
        c2, gun_d, pmax = snap_peak_to_gunfire(
            video_path,
            center,
            duration=dur,
            confirm_panns=not audio_generator,
            pcm_cache=pcm_cache,
        )
        panns_use = max(panns_g, pmax)
        if gun_d < dens_min and panns_use < gun_min * 1.05:
            log.info(
                "dense snap drop center=%.0f→%.0f gun=%.3f panns=%.3f",
                center,
                c2,
                gun_d,
                panns_use,
            )
            continue
        snapped.append((c2, gun_d, panns_use))

    if funnel is not None:
        funnel.snapped = len(snapped)

    snapped.sort(key=lambda x: -(x[1] * 2.0 + x[2]))
    scored_centers = [
        (float(panns_g) * 2.0 + float(gun_d), float(center)) for center, gun_d, panns_g in snapped
    ]
    picked: list[float] = []
    picked_scores: list[float] = []
    from vod_montage_cluster import pool_peak_gap_sec, sequential_montage_enabled, sequential_pool_peaks

    if sequential_montage_enabled():
        picked = sequential_pool_peaks(scored_centers, pool_cap=pool_cap)
        picked_scores = [0.0] * len(picked)
        pick_gap = pool_peak_gap_sec()
    else:
        pick_gap = gap_sec * 0.85
        for center, gun_d, panns_g in snapped:
            if any(abs(center - p) < pick_gap for p in picked):
                continue
            picked.append(center)
            picked_scores.append(float(gun_d) * 2.0 + float(panns_g))
            if len(picked) >= pool_cap:
                break

    if len(picked) < min_clips and snapped:
        ultra = max(12.0, gap_sec * 0.35)
        if sequential_montage_enabled():
            picked = sequential_pool_peaks(scored_centers, pool_cap=pool_cap, part_gap_sec=ultra)
            picked_scores = [0.0] * len(picked)
            pick_gap = ultra
        elif ultra < pick_gap:
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

    if funnel is not None:
        funnel.picked = len(picked)

    top = scored[0][0] if scored else 0.0
    reason = (
        f"{'sequential' if sequential_montage_enabled() else 'spread'} "
        f"{'audio_generator' if audio_generator else 'dense_panns'} "
        f"hits={len(scored)}/{len(offsets)} shortlist={len(shortlist)} "
        f"snapped={len(snapped)} picked={len(picked)} gap={pick_gap:.0f} top={top:.3f} pass={probe_pass}"
    )
    if batch_stats:
        reason += (
            f" batch=dsp{batch_stats.get('dsp_pass', 0)}"
            f"/panns{batch_stats.get('panns_windows', 0)}"
        )
    # Combat timeline: merge seeds into duration-scaled events (no fixed top-3).
    # Keeps fights across the whole VOD, including the tail.
    try:
        from pubg_combat_timeline import refine_peaks_with_timeline, summarize_events, timeline_enabled

        if timeline_enabled() and picked:
            before_n = len(picked)
            refined = refine_peaks_with_timeline(
                picked,
                duration_sec=dur,
                scores=picked_scores or None,
            )
            if refined:
                picked = refined
                # Scores optional after merge — keep length aligned for cache.
                if len(picked_scores) != len(picked):
                    picked_scores = [0.0] * len(picked)
                reason += f" timeline={len(picked)}/{before_n}"
                log.info(
                    "combat timeline refine vod=%s before=%s after=%s span=%.0f-%.0f",
                    video_path.name,
                    before_n,
                    len(picked),
                    picked[0] if picked else -1,
                    picked[-1] if picked else -1,
                )
    except Exception:
        log.exception("combat timeline refine failed vod=%s", video_path.name)

    log.info("dense gun peaks vod=%s %s peaks=%s", video_path.name, reason, picked[:8])

    try:
        from vod_peak_feature_cache import put_cached

        features = [
            {"peak_sec": round(float(p), 1), "score": round(float(s), 4)}
            for p, s in zip(picked, picked_scores)
        ]
        put_cached(
            video_path,
            probe_pass,
            peaks=picked,
            reason=reason,
            features=features,
            funnel=funnel.to_dict() if funnel is not None and hasattr(funnel, "to_dict") else None,
            timings=batch_stats or None,
        )
    except Exception:
        pass

    return picked, reason


def apply_fast_probe_seeds(peaks: list[float]) -> None:
    if not peaks or os.environ.get("SHOOTER_VOD_SEED_FROM_FAST_PROBE", "1") != "1":
        return
    os.environ["HIGHLIGHT_ALLOW_SEED_STARTS"] = "1"
    # Full peak scan: seed every discovered moment, not a silent top-8.
    if os.environ.get("PUBG_FULL_PEAK_SCAN", "1") == "1":
        seed_peaks = list(peaks)
    else:
        seed_peaks = list(peaks[:8])
    os.environ["HIGHLIGHT_SEED_STARTS"] = ",".join(str(round(p, 1)) for p in seed_peaks)


def clear_fast_probe_seeds() -> None:
    os.environ.pop("HIGHLIGHT_SEED_STARTS", None)
    os.environ.pop("HIGHLIGHT_ALLOW_SEED_STARTS", None)
