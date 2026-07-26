#!/usr/bin/env python3
"""MLBB VOD multi-moment montage: more peaks from one VOD, less empty running."""

from __future__ import annotations

import logging
import os
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

log = logging.getLogger("mlbb_vod_montage")


def montage_enabled() -> bool:
    if os.environ.get("MLBB_SKIP_MONTAGE", "0") == "1":
        return False
    return os.environ.get("MLBB_VOD_MONTAGE", "0") == "1"


def montage_min_clips() -> int:
    return max(2, int(os.environ.get("MLBB_VOD_MONTAGE_MIN_CLIPS", "3")))


def montage_max_clips() -> int:
    return max(montage_min_clips(), int(os.environ.get("MLBB_VOD_MONTAGE_MAX_CLIPS", "4")))


def montage_gap_sec() -> float:
    return float(os.environ.get("MLBB_VOD_MONTAGE_GAP_SEC", "45"))


def montage_target_sec() -> tuple[float, float]:
    lo = float(os.environ.get("MLBB_VOD_MONTAGE_MIN_SEC", "32"))
    hi = float(os.environ.get("MLBB_VOD_MONTAGE_MAX_SEC", "70"))
    return lo, hi


@contextmanager
def montage_collect_env() -> Iterator[None]:
    """During collect: allow single-kill banners; still require a banner."""
    if not montage_enabled():
        yield
        return
    keys = {
        "MLBB_KILL_BANNER_MIN_TIER": os.environ.get(
            "MLBB_VOD_MONTAGE_MIN_TIER",
            "single",
        ),
        "MLBB_KILL_BANNER_REQUIRED": "1",
        "MLBB_VOD_MOTION_ANCHOR_OK": "0",
        "MLBB_VOD_BANNER_DISCOVER": os.environ.get("MLBB_VOD_BANNER_DISCOVER", "1"),
        "MLBB_VOD_BANNER_PRESEND": os.environ.get("MLBB_VOD_BANNER_PRESEND", "1"),
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
    banner_sec: float | None = None,
) -> float:
    """
    Cut post-fight running: after banner/peak, if combat energy dies while
    center motion stays high (sprint/recall), trim the idle tail.
    """
    if os.environ.get("MLBB_VOD_TRIM_RUN", "1") != "1":
        return end
    try:
        from mlbb_fight_segment import _analysis_for
        import numpy as np
    except Exception:
        return end

    analysis = _analysis_for(vod)
    win = float(analysis.get("window_seconds") or 2.0)
    file_dur = float(analysis.get("duration") or 0.0)
    bins = int(analysis.get("bins") or 0)
    if bins < 4 or win <= 0 or end <= start + 6:
        return end

    motion = np.asarray(analysis["center_motion"], dtype=np.float32)
    audio = np.asarray(analysis["audio"], dtype=np.float32)
    scene_raw = analysis.get("scene")
    if scene_raw is None:
        scene = audio
    else:
        scene = np.asarray(scene_raw, dtype=np.float32)
    combat = audio * 0.55 + scene * 0.45

    anchor = float(banner_sec if banner_sec is not None else start + (end - start) * 0.45)
    anchor_idx = int(round(anchor / win))
    end_idx = min(bins - 1, int(round(end / win)))
    start_idx = max(0, int(round(start / win)))
    if end_idx <= anchor_idx + 1:
        return end

    # Thresholds from the fight body (around banner), not the whole clip —
    # otherwise a long run-tail pulls combat_thr down and never triggers.
    fight_hi = min(end_idx, max(anchor_idx + 1, start_idx + 1))
    fight_slice = combat[start_idx : fight_hi + 1]
    motion_slice = motion[start_idx : fight_hi + 1]
    if fight_slice.size < 2:
        fight_slice = combat[start_idx : end_idx + 1]
        motion_slice = motion[start_idx : end_idx + 1]
    combat_ref = float(np.median(fight_slice)) if fight_slice.size else float(combat.mean())
    motion_ref = float(np.median(motion_slice)) if motion_slice.size else float(motion.mean())
    combat_thr = max(combat_ref * 0.55, float(np.percentile(combat[start_idx : end_idx + 1], 25)))
    motion_thr = max(motion_ref * 0.85, float(np.percentile(motion[start_idx : end_idx + 1], 50)))
    quiet_need = max(2, int(os.environ.get("MLBB_VOD_RUN_QUIET_BINS", "2")))
    min_post = float(os.environ.get("MLBB_BANNER_POST_SEC", "4"))

    quiet = 0
    cut_idx = end_idx
    for idx in range(anchor_idx + 1, end_idx + 1):
        low_combat = combat[idx] < combat_thr * 0.92
        high_motion = motion[idx] >= motion_thr * 0.90
        # Running / recall: still moving, fight audio/UI gone.
        if low_combat and high_motion:
            quiet += 1
            if quiet >= quiet_need:
                cut_idx = max(anchor_idx + 1, idx - quiet_need + 1)
                break
        elif low_combat and not high_motion:
            quiet += 1
            if quiet >= quiet_need:
                cut_idx = max(anchor_idx + 1, idx - quiet_need + 1)
                break
        else:
            quiet = 0

    new_end = min(file_dur if file_dur > 0 else end, (cut_idx + 1) * win)
    new_end = max(new_end, anchor + min_post)
    new_end = min(new_end, end)
    # Never delete more than half the fight window — over-trim caused mid-fight chops.
    max_save = max(4.0, (end - start) * float(os.environ.get("MLBB_VOD_TRIM_MAX_FRAC", "0.35")))
    if end - new_end > max_save:
        new_end = end - max_save
        new_end = max(new_end, anchor + min_post)
        new_end = min(new_end, end)
    if new_end < end - 0.4:
        log.info(
            "trim run tail vod=%s %.1f→%.1f (saved %.1fs)",
            vod.name,
            end,
            new_end,
            end - new_end,
        )
    return round(new_end, 2)


def apply_run_trim_to_clip(clip: dict, vod: Path) -> dict:
    start = float(clip.get("start") or 0)
    dur = float(clip.get("input_duration") or 0)
    if dur < 6:
        return clip
    # Motion-anchor peaks are not kill banners — aggressive tail trim chops real fights.
    if str(clip.get("anchor") or "") == "motion" and os.environ.get("MLBB_VOD_TRIM_MOTION_ANCHOR", "0") != "1":
        return clip
    end = start + dur
    banner = clip.get("peak_start", clip.get("banner_sec", start + dur * 0.4))
    new_end = trim_idle_run_end(vod, start, end, banner_sec=float(banner or start))
    new_dur = max(float(os.environ.get("MLBB_FIGHT_MIN_SEC", "7")), new_end - start)
    if abs(new_dur - dur) < 0.3:
        return clip
    out = dict(clip)
    out["input_duration"] = round(new_dur, 2)
    out["output_duration"] = round(new_dur, 2)
    out["fight_end"] = round(start + new_dur, 2)
    return out


def _montage_timeline_key(row: dict) -> float:
    """VOD timeline position — peak/banner time, not clip window start."""
    return float(row.get("peak_start", row.get("banner_sec", row.get("start") or 0)) or 0)


def pick_montage_rows(rows: list[dict]) -> list[dict]:
    """Pick 2–4 spaced peaks; prefer a double+ when available, singles allowed."""
    if not rows:
        return []
    # Never stitch motion-only soften clips — that produces jumpy "кривая нарезка".
    bannered = [
        r
        for r in rows
        if int(r.get("kill_banner_tier") or 0) > 0
        or r.get("kill_banner")
        or str(r.get("anchor") or "") not in {"", "motion"}
    ]
    if len(bannered) < montage_min_clips():
        log.info("montage skip — need >=%s bannered fights (have %s)", montage_min_clips(), len(bannered))
        return []
    rows = bannered
    min_n = montage_min_clips()
    max_n = montage_max_clips()
    gap = montage_gap_sec()

    ranked = sorted(
        rows,
        key=lambda r: (
            int(r.get("kill_banner_tier") or 0),
            float(r.get("clip_score") or 0),
            float(r.get("score") or 0),
        ),
        reverse=True,
    )
    chosen: list[dict] = []
    # Seed with best multi-kill if any.
    for row in ranked:
        tier = int(row.get("kill_banner_tier") or 0)
        if tier >= 2:
            chosen.append(row)
            break
    for row in ranked:
        if row in chosen:
            continue
        peak = float(row.get("peak_start", row.get("start") or 0))
        if any(abs(peak - float(c.get("peak_start", c.get("start") or 0))) < gap for c in chosen):
            continue
        chosen.append(row)
        if len(chosen) >= max_n:
            break
    chosen.sort(key=_montage_timeline_key)
    if len(chosen) < min_n:
        return []
    lo, hi = montage_target_sec()
    est = sum(float(r.get("fight_dur") or r.get("clip", {}).get("input_duration") or 12) for r in chosen)
    xfade = float(os.environ.get("TRANSITION_DURATION", "0.28"))
    est -= xfade * max(0, len(chosen) - 1)
    while len(chosen) > min_n and est > hi:
        # Drop the latest moment so earlier chronology stays intact.
        chosen.pop()
        est = sum(float(r.get("fight_dur") or 12) for r in chosen) - xfade * max(0, len(chosen) - 1)
    if est < lo and len(chosen) < max_n:
        # keep as-is — short montage still better than spam
        pass
    # Final guarantee: match timeline order (never score/tier order).
    chosen.sort(key=_montage_timeline_key)
    return chosen


def build_montage_id(vod_id: str, rows: list[dict]) -> str:
    peaks = "_".join(str(int(float(r.get("peak_start", r.get("start") or 0)))) for r in rows[:4])
    return f"{vod_id}_m{peaks}"


def render_concat_montage(
    vod: Path,
    rows: list[dict],
    out_path: Path,
    *,
    render_fn,
) -> tuple[bool, list[Path]]:
    """Render each segment, xfade-concat into one file. Returns (ok, temp paths)."""
    temps: list[Path] = []
    durations: list[float] = []
    try:
        for i, row in enumerate(rows):
            clip = apply_run_trim_to_clip(dict(row.get("clip") or row), vod)
            tmp = Path(tempfile.mkstemp(suffix=f".m{i}.mp4")[1])
            temps.append(tmp)
            if not render_fn(vod, clip, tmp):
                log.warning("montage render fail part=%s", i)
                return False, temps
            dur = float(clip.get("input_duration") or 0)
            if dur < 1:
                from smart_video_editor import ffprobe_duration

                dur = ffprobe_duration(tmp)
            durations.append(dur)
        ok = concat_rendered_parts(temps, durations, out_path)
        return ok, temps
    except Exception:
        log.exception("montage concat failed")
        return False, temps


def concat_rendered_parts(
    parts: list[Path],
    durations: list[float],
    out_path: Path,
) -> bool:
    """Xfade already-rendered part files into one montage mp4."""
    from smart_video_editor import build_xfade_command, run_command

    if not parts:
        return False
    try:
        if len(parts) == 1:
            out_path.write_bytes(parts[0].read_bytes())
            return out_path.exists() and out_path.stat().st_size > 100_000
        cmd = build_xfade_command(parts, durations, out_path)
        run_command(cmd)
        return out_path.exists() and out_path.stat().st_size > 100_000
    except Exception:
        log.exception("montage xfade failed")
        return False


def cleanup_temps(paths: list[Path]) -> None:
    for p in paths:
        try:
            p.unlink(missing_ok=True)
        except OSError:
            pass
