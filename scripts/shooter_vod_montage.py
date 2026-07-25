#!/usr/bin/env python3
"""Shooter (PUBG/Standoff) multi-moment montage: more fights from one VOD, less running."""

from __future__ import annotations

import logging
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

log = logging.getLogger("shooter_vod_montage")


def montage_enabled(game: str = "") -> bool:
    """Enable via SHOOTER_VOD_MONTAGE=1, or game-specific PUBG/STANDOFF flags.

    Standoff defaults ON — merge several fights into one video.
    """
    if os.environ.get("SHOOTER_VOD_MONTAGE", "0") == "1":
        return True
    g = (game or "").strip().lower()
    if g == "pubg" and os.environ.get("PUBG_VOD_MONTAGE", "0") == "1":
        return True
    if g == "standoff" and os.environ.get("STANDOFF_VOD_MONTAGE", "1") == "1":
        return True
    return False


def montage_min_clips() -> int:
    return max(2, int(os.environ.get("SHOOTER_VOD_MONTAGE_MIN_CLIPS", "2")))


def montage_max_clips() -> int:
    return max(montage_min_clips(), int(os.environ.get("SHOOTER_VOD_MONTAGE_MAX_CLIPS", "4")))


def montage_gap_sec() -> float:
    return float(os.environ.get("SHOOTER_VOD_MONTAGE_GAP_SEC", "55"))


def montage_target_sec() -> tuple[float, float]:
    lo = float(os.environ.get("SHOOTER_VOD_MONTAGE_MIN_SEC", "22"))
    hi = float(os.environ.get("SHOOTER_VOD_MONTAGE_MAX_SEC", "48"))
    return lo, hi


@contextmanager
def montage_collect_env(game: str = "") -> Iterator[None]:
    """
    During pool walk: keep combat gates, but accept slightly weaker clips
    (PUBG "singles" ≈ short/valid fights, not loot runs).
    """
    if not montage_enabled(game):
        yield
        return
    keys = {
        "SHOOTER_VOD_MIN_CLIP_SCORE": os.environ.get(
            "SHOOTER_VOD_MONTAGE_MIN_CLIP_SCORE",
            "0.02",
        ),
        # Slightly softer gunfire floor while collecting montage parts only.
        "SMART_PUBG_BIN_GUNFIRE_MIN": os.environ.get(
            "SHOOTER_VOD_MONTAGE_GUNFIRE_MIN",
            os.environ.get("SMART_PUBG_BIN_GUNFIRE_MIN", "0.06"),
        ),
        "SMART_PUBG_MIN_BIN_GUNFIRE_PEAK": os.environ.get(
            "SHOOTER_VOD_MONTAGE_GUNFIRE_PEAK",
            os.environ.get("SMART_PUBG_MIN_BIN_GUNFIRE_PEAK", "0.09"),
        ),
    }
    saved = {k: os.environ.get(k) for k in keys}
    try:
        os.environ.update(keys)
        yield
    finally:
        for k, prev in saved.items():
            if prev is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = prev


def trim_idle_run_end(
    vod: Path,
    start: float,
    end: float,
    *,
    peak_sec: float | None = None,
) -> float:
    """
    Cut post-fight running: after peak, when gunfire dies but center motion
    stays high (sprint / loot jog), trim the idle tail.
    """
    if os.environ.get("SHOOTER_VOD_TRIM_RUN", "1") != "1":
        return end
    try:
        import numpy as np
        from vod_analysis_cache import analyze_video_cached
    except Exception:
        return end

    try:
        analysis = analyze_video_cached(vod)
    except Exception:
        return end

    win = float(analysis.get("window_seconds") or 1.0)
    file_dur = float(analysis.get("duration") or 0.0)
    bins = int(analysis.get("bins") or 0)
    if bins < 4 or win <= 0 or end <= start + 4:
        return end

    motion_raw = analysis.get("center_motion")
    if motion_raw is None:
        motion_raw = analysis.get("motion")
    motion = np.asarray(motion_raw, dtype=np.float32)
    gun = analysis.get("gunfire")
    if gun is None:
        gun = analysis.get("audio")
    gunfire = np.asarray(gun, dtype=np.float32)
    if motion.size < 4 or gunfire.size < 4:
        return end

    anchor = float(peak_sec if peak_sec is not None else start + (end - start) * 0.4)
    anchor_idx = int(round(anchor / win))
    end_idx = min(bins - 1, int(round(end / win)))
    start_idx = max(0, int(round(start / win)))
    if end_idx <= anchor_idx + 1:
        return end

    fight_hi = min(end_idx, max(anchor_idx + 1, start_idx + 1))
    fight_gun = gunfire[start_idx : fight_hi + 1]
    fight_mot = motion[start_idx : fight_hi + 1]
    if fight_gun.size < 2:
        fight_gun = gunfire[start_idx : end_idx + 1]
        fight_mot = motion[start_idx : end_idx + 1]
    gun_ref = float(np.median(fight_gun)) if fight_gun.size else float(gunfire.mean())
    mot_ref = float(np.median(fight_mot)) if fight_mot.size else float(motion.mean())
    gun_thr = max(gun_ref * 0.45, float(np.percentile(gunfire[start_idx : end_idx + 1], 25)))
    mot_thr = max(mot_ref * 0.85, float(np.percentile(motion[start_idx : end_idx + 1], 50)))
    quiet_need = max(2, int(os.environ.get("SHOOTER_VOD_RUN_QUIET_BINS", "2")))
    min_post = float(os.environ.get("SHOOTER_VOD_FIGHT_POST_SEC", "2.5"))

    quiet = 0
    cut_idx = end_idx
    for idx in range(anchor_idx + 1, end_idx + 1):
        low_gun = gunfire[idx] < gun_thr * 0.90
        high_motion = motion[idx] >= mot_thr * 0.88
        if low_gun and high_motion:
            quiet += 1
            if quiet >= quiet_need:
                cut_idx = max(anchor_idx + 1, idx - quiet_need + 1)
                break
        elif low_gun and not high_motion:
            quiet += 1
            if quiet >= quiet_need:
                cut_idx = max(anchor_idx + 1, idx - quiet_need + 1)
                break
        else:
            quiet = 0

    new_end = min(file_dur if file_dur > 0 else end, (cut_idx + 1) * win)
    new_end = max(new_end, anchor + min_post)
    new_end = min(new_end, end)
    if new_end < end - 0.35:
        log.info(
            "trim run tail vod=%s %.1f→%.1f (saved %.1fs)",
            vod.name,
            end,
            new_end,
            end - new_end,
        )
    return round(new_end, 2)


def apply_run_trim_to_clip(clip: dict, vod: Path, *, game: str = "pubg") -> dict:
    start = float(clip.get("start") or 0)
    dur = float(clip.get("input_duration") or clip.get("output_duration") or 0)
    if dur < 4:
        if game == "standoff":
            dur = float(os.environ.get("SMART_STANDOFF_CLIP_MAX_SEC", "9.0"))
        else:
            dur = float(os.environ.get("SMART_PUBG_CLIP_MAX_SEC", "9.5"))
    end = start + dur
    peak = float(clip.get("peak_start", start + dur * 0.4))
    new_end = trim_idle_run_end(vod, start, end, peak_sec=peak)
    min_dur = float(os.environ.get("SMART_PUBG_CLIP_MIN_SEC", "6.0"))
    new_dur = max(min_dur, new_end - start)
    if abs(new_dur - dur) < 0.25:
        return clip
    out = dict(clip)
    out["input_duration"] = round(new_dur, 2)
    out["output_duration"] = round(new_dur, 2)
    out["fight_end"] = round(start + new_dur, 2)
    return out


def pick_montage_rows(rows: list[dict]) -> list[dict]:
    """Pick 2–4 spaced fight peaks; prefer killfeed/gun/POV evidence over raw score."""
    if not rows:
        return []
    min_n = montage_min_clips()
    max_n = montage_max_clips()
    gap = montage_gap_sec()

    def _precision_key(r: dict) -> tuple:
        hm = r.get("highlight_metrics") or r.get("clip", {}).get("highlight_metrics") or {}
        kf = float(r.get("killfeed_density") or hm.get("ocr_hits") or hm.get("killfeed_density") or 0)
        gun_q = float(r.get("gunfire_quarters_active") or hm.get("gunfire_quarters_active") or 0)
        gun_c = float(r.get("gunfire_clusters") or hm.get("gunfire_clusters") or 0)
        panns = float(r.get("panns_gun_max") or hm.get("panns_gun_max") or 0)
        clip_score = float(r.get("clip_score") or hm.get("clip_score") or 0)
        score = float(r.get("score") or 0)
        # Penalize sniper-hold / audio-only-ish rows when marked.
        penalty = 0.0
        if str(hm.get("pass_reason") or "").startswith("sniper"):
            penalty -= 0.5
        return (kf, gun_q + gun_c * 0.5, panns, clip_score + penalty, score)

    ranked = sorted(rows, key=_precision_key, reverse=True)
    chosen: list[dict] = []
    for row in ranked:
        peak = float(row.get("peak_start", row.get("start") or 0))
        if any(abs(peak - float(c.get("peak_start", c.get("start") or 0))) < gap for c in chosen):
            continue
        chosen.append(row)
        if len(chosen) >= max_n:
            break
    chosen.sort(key=lambda r: float(r.get("start") or 0))
    if len(chosen) < min_n:
        return []
    lo, hi = montage_target_sec()
    xfade = float(os.environ.get("TRANSITION_DURATION", "0.28"))
    est = sum(float(r.get("fight_dur") or r.get("clip", {}).get("input_duration") or 8) for r in chosen)
    est -= xfade * max(0, len(chosen) - 1)
    while len(chosen) > min_n and est > hi:
        chosen.pop()
        est = sum(float(r.get("fight_dur") or 8) for r in chosen) - xfade * max(0, len(chosen) - 1)
    if est < lo:
        pass
    return chosen


def build_montage_id(vod_id: str, rows: list[dict]) -> str:
    peaks = "_".join(str(int(float(r.get("peak_start", r.get("start") or 0)))) for r in rows[:4])
    return f"{vod_id}_m{peaks}"


def concat_rendered_parts(
    parts: list[Path],
    durations: list[float],
    out_path: Path,
) -> bool:
    """Reuse MLBB xfade helper so PUBG/MLBB montage share one concat path."""
    from mlbb_vod_montage import concat_rendered_parts as _concat

    return _concat(parts, durations, out_path)


def cleanup_temps(paths: list[Path]) -> None:
    for p in paths:
        try:
            p.unlink(missing_ok=True)
        except OSError:
            pass


def vod_richness_rank(row: dict) -> int:
    """Lower is better — stick to VODs with leftover fight peaks."""
    if row.get("last_scan_blocked"):
        return 2
    peaks = row.get("last_pool_peaks")
    if not isinstance(peaks, list) or not peaks:
        return 1 if float(row.get("last_scan_at") or 0) > 0 else 0
    pool_n = len(peaks)
    zero = int(row.get("zero_send_attempts") or 0)
    if pool_n >= 3 and zero < 2:
        return -1
    if pool_n >= 1 and zero < 3:
        return 0
    return 1
