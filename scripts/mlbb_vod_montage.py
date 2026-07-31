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


def _banner_post_sec(banner_tier: int | None = None) -> float:
    try:
        from mlbb_fight_segment import banner_post_sec

        return float(banner_post_sec(banner_tier))
    except Exception:
        return float(os.environ.get("MLBB_BANNER_POST_SEC", "3"))


def montage_enabled() -> bool:
    if os.environ.get("MLBB_SKIP_MONTAGE", "0") == "1":
        return False
    return os.environ.get("MLBB_VOD_MONTAGE", "0") == "1"


def montage_min_clips() -> int:
    # Reliable quota path ships 1 own-kill moment; do not force a 2-clip floor.
    return max(1, int(os.environ.get("MLBB_VOD_MONTAGE_MIN_CLIPS", "3")))


def montage_max_clips() -> int:
    return max(montage_min_clips(), int(os.environ.get("MLBB_VOD_MONTAGE_MAX_CLIPS", "4")))


def montage_gap_sec() -> float:
    return float(os.environ.get("MLBB_VOD_MONTAGE_GAP_SEC", "45"))


def montage_allow_singles() -> bool:
    """Stitch several single-kill moments when no double+ is available."""
    return os.environ.get("MLBB_VOD_MONTAGE_ALLOW_SINGLES", "1") == "1"


def _row_banner_source(row: dict) -> str:
    return str(
        row.get("banner_source")
        or row.get("kill_banner_source")
        or (row.get("clip") or {}).get("banner_source")
        or ""
    )


def montage_single_row_ok(row: dict) -> bool:
    """Single OK for montage: prefer ref; OCR singles only when explicitly allowed."""
    tier = int(row.get("kill_banner_tier") or 0)
    if tier < 1:
        return False
    if tier >= 2:
        return True
    # HUD-confirmed own-kill singles (wb0) — ship when montage is thin.
    own = str(row.get("own_kill_recheck") or row.get("own_kill_reason") or "")
    if own.startswith("hud_killer_ok") and os.environ.get(
        "MLBB_PRESEND_OWN_KILL_SINGLE", "1"
    ) == "1":
        return True
    src = _row_banner_source(row)
    label = str(row.get("kill_banner") or "").lower()
    allow_ocr = (
        os.environ.get("MLBB_VOD_MONTAGE_ALLOW_OCR_SINGLE", "0") == "1"
        or os.environ.get("MLBB_ADAPTIVE_ALLOW_SINGLE", "0") == "1"
    )
    if src.startswith("ocr") or label in {"single_weak", "color", "announce"}:
        if not allow_ocr:
            return False
        # Still reject garbled OCR labels.
        if label in {"single_weak", "color", "announce"}:
            return False
        return True
    if os.environ.get("MLBB_BANNER_REJECT_OCR_SINGLE", "1") == "1" and not src:
        # Empty source + single label — treat as OCR unless allow flag set.
        if not allow_ocr:
            return src.startswith("ref")
    return src.startswith("ref") or label in {"single", "double", "triple", "maniac", "savage"}


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
    banner_tier: int | None = None,
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
    try:
        min_post = _banner_post_sec(banner_tier)
    except Exception:
        min_post = float(os.environ.get("MLBB_BANNER_POST_SEC", "3"))

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
    # Absolute cap: stop after the kill banner (tier-aware post — keep combo kills).
    if os.environ.get("MLBB_BANNER_HARD_POST_CUT", "1") == "1":
        post = _banner_post_sec(banner_tier)
        hard = anchor + post
        if new_end > hard:
            new_end = min(new_end, hard, end)
            new_end = max(new_end, anchor + min(post, min_post))
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
    banner = float(clip.get("peak_start", clip.get("banner_sec", start + dur * 0.4)) or start)
    tier = int(clip.get("kill_banner_tier") or clip.get("banner_tier") or 0)
    new_end = trim_idle_run_end(
        vod, start, end, banner_sec=banner, banner_tier=tier or None
    )
    # Always hard-cap after kill banner, even if run-trim heuristics found nothing.
    if os.environ.get("MLBB_BANNER_HARD_POST_CUT", "1") == "1":
        post = _banner_post_sec(tier or None)
        new_end = min(new_end, banner + post, end)
        new_end = max(new_end, banner + min(1.5, post))
    new_dur = max(float(os.environ.get("MLBB_FIGHT_MIN_SEC", "7")), new_end - start)
    # If min duration would re-introduce run, prefer shorter clip over run tail.
    if os.environ.get("MLBB_BANNER_HARD_POST_CUT", "1") == "1":
        post = _banner_post_sec(tier or None)
        hard_dur = max(4.0, (banner + post) - start)
        new_dur = min(new_dur, hard_dur) if hard_dur >= 4.0 else new_dur
    if abs(new_dur - dur) < 0.3:
        return clip
    out = dict(clip)
    out["input_duration"] = round(new_dur, 2)
    out["output_duration"] = round(new_dur, 2)
    out["fight_end"] = round(start + new_dur, 2)
    return out


def clip_run_fraction(
    vod: Path,
    start: float,
    end: float,
    *,
    banner_sec: float | None = None,
) -> float:
    """
    Share of the window that looks like post-fight / lane jogging:
    low combat energy (audio+scene) while center motion stays elevated.
    Returns 0..1. Used to reject montage parts that are mostly running.
    """
    if end <= start + 4:
        return 0.0
    try:
        from mlbb_fight_segment import _analysis_for
        import numpy as np
    except Exception:
        return 0.0

    try:
        analysis = _analysis_for(vod)
    except Exception:
        return 0.0

    win = float(analysis.get("window_seconds") or 2.0)
    bins = int(analysis.get("bins") or 0)
    if bins < 4 or win <= 0:
        return 0.0

    motion = np.asarray(analysis["center_motion"], dtype=np.float32)
    audio = np.asarray(analysis["audio"], dtype=np.float32)
    scene_raw = analysis.get("scene")
    scene = audio if scene_raw is None else np.asarray(scene_raw, dtype=np.float32)
    combat = audio * 0.55 + scene * 0.45

    start_idx = max(0, int(round(start / win)))
    end_idx = min(bins - 1, int(round(end / win)))
    if end_idx <= start_idx + 1:
        return 0.0

    # Protect a short core around the banner — don't count the kill flash as run.
    anchor = float(banner_sec if banner_sec is not None else start + (end - start) * 0.4)
    core_pad = float(os.environ.get("MLBB_RUN_FRAC_CORE_PAD_SEC", "3.0"))
    core_lo = max(start_idx, int(round((anchor - core_pad) / win)))
    core_hi = min(end_idx, int(round((anchor + core_pad) / win)))

    combat_slice = combat[start_idx : end_idx + 1]
    motion_slice = motion[start_idx : end_idx + 1]
    if combat_slice.size < 3:
        return 0.0
    combat_thr = max(
        float(np.median(combat_slice)) * 0.70,
        float(np.percentile(combat_slice, 30)),
    )
    motion_thr = max(
        float(np.median(motion_slice)) * 0.80,
        float(np.percentile(motion_slice, 40)),
    )

    run_bins = 0
    total = 0
    for idx in range(start_idx, end_idx + 1):
        if core_lo <= idx <= core_hi:
            continue
        total += 1
        # Inclusive threshold: median combat bins must still count as "low".
        low_combat = float(combat[idx]) <= combat_thr * 1.05
        high_motion = float(motion[idx]) >= motion_thr * 0.85
        if low_combat and high_motion:
            run_bins += 1
        elif low_combat and float(motion[idx]) < motion_thr * 0.55:
            # Standing still after fight / recall channel — also dead air.
            run_bins += 1
    if total <= 0:
        return 0.0
    return float(run_bins) / float(total)


def _montage_timeline_key(row: dict) -> float:
    """VOD timeline position — peak/banner time, not clip window start."""
    return float(row.get("peak_start", row.get("banner_sec", row.get("start") or 0)) or 0)


def _is_bannered_row(row: dict) -> bool:
    return bool(
        int(row.get("kill_banner_tier") or 0) > 0
        or row.get("kill_banner")
        or str(row.get("anchor") or "") not in {"", "motion"}
    )


def bannered_rows(rows: list[dict]) -> list[dict]:
    return [r for r in rows if _is_bannered_row(r)]


def shippable_bannered_rows(rows: list[dict], *, min_tier: int | None = None) -> list[dict]:
    """Bannered fights that meet the SEND floor (not just collect/discover floor)."""
    if min_tier is None:
        try:
            from mlbb_kill_banner import send_min_tier

            min_tier = send_min_tier()
        except Exception:
            min_tier = 2
    out: list[dict] = []
    for row in bannered_rows(rows):
        tier = int(row.get("kill_banner_tier") or 0)
        if tier >= min_tier:
            out.append(row)
    return out


def montage_eligible_rows(rows: list[dict]) -> list[dict]:
    """
    Fights eligible for montage stitching.

    Prefer double+; when none exist, ref-backed singles can fill a montage
  instead of wasting the whole VOD.
    """
    shippable = shippable_bannered_rows(rows)
    if not montage_allow_singles():
        return shippable
    out = list(shippable)
    seen = {id(r) for r in out}
    for row in bannered_rows(rows):
        if id(row) in seen:
            continue
        if montage_single_row_ok(row):
            out.append(row)
            seen.add(id(row))
    return out


def pick_montage_rows(rows: list[dict]) -> list[dict]:
    """Pick 2–4 spaced peaks; prefer double+, stitch singles when no double+."""
    if not rows:
        return []
    # Never stitch motion-only soften clips — that produces jumpy "кривая нарезка".
    soft_fallback = os.environ.get("MLBB_VOD_MONTAGE_SOFT_FALLBACK", "1") == "1"
    soft_min = max(2, int(os.environ.get("MLBB_VOD_MONTAGE_SOFT_MIN", "2")))
    min_n = montage_min_clips()
    shippable = shippable_bannered_rows(rows)
    eligible = montage_eligible_rows(rows)
    if len(shippable) >= min_n:
        pool = shippable
        pool_label = "double+"
    elif len(eligible) >= min_n and montage_allow_singles():
        pool = eligible
        pool_label = "singles"
        log.info(
            "montage singles — no double+ floor, stitch %s ref-backed moments (wanted %s)",
            len(eligible),
            min_n,
        )
    elif soft_fallback and len(shippable) >= soft_min:
        pool = shippable
        pool_label = "double+soft"
    elif soft_fallback and len(eligible) >= soft_min and montage_allow_singles():
        pool = eligible
        pool_label = "singles-soft"
        log.info(
            "montage soft singles — stitch %s moments (wanted %s)",
            len(eligible),
            min_n,
        )
    else:
        log.info(
            "montage skip — need >=%s eligible fights (double+=%s eligible=%s)",
            min_n,
            len(shippable),
            len(eligible),
        )
        return []
    rows = pool
    max_n = montage_max_clips()
    gap = montage_gap_sec()
    effective_min = min_n if len(pool) >= min_n else soft_min

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
    if len(chosen) < effective_min and gap > 18.0:
        # Close fights (22s dense hit gap) must still montage — retry with tighter spacing.
        tight = max(12.0, gap * 0.5)
        chosen = []
        for row in ranked:
            if row in chosen:
                continue
            peak = float(row.get("peak_start", row.get("start") or 0))
            if any(abs(peak - float(c.get("peak_start", c.get("start") or 0))) < tight for c in chosen):
                continue
            chosen.append(row)
            if len(chosen) >= max_n:
                break
        chosen.sort(key=_montage_timeline_key)
        log.info(
            "montage tight-gap retry gap=%.0f→%.0f picked=%s need=%s",
            gap,
            tight,
            len(chosen),
            effective_min,
        )
    if len(chosen) < effective_min:
        log.info(
            "montage skip — %s pool=%s picked=%s need>=%s",
            pool_label,
            len(pool),
            len(chosen),
            effective_min,
        )
        return []
    lo, hi = montage_target_sec()
    est = sum(float(r.get("fight_dur") or r.get("clip", {}).get("input_duration") or 12) for r in chosen)
    xfade = float(os.environ.get("TRANSITION_DURATION", "0.28"))
    est -= xfade * max(0, len(chosen) - 1)
    while len(chosen) > effective_min and est > hi:
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
