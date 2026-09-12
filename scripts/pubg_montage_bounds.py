#!/usr/bin/env python3
"""Shared PUBG montage fight-window logic — main feed + owner redo."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

log = logging.getLogger("pubg_montage_bounds")


def min_fight_window_gap_sec() -> float:
    return float(
        os.environ.get(
            "PUBG_MONTAGE_MIN_WINDOW_GAP_SEC",
            os.environ.get("PUBG_OWNER_REDO_MIN_WINDOW_GAP_SEC", "30"),
        )
    )


def clip_pre_shoot_sec() -> float:
    return float(
        os.environ.get(
            "PUBG_CLIP_PRE_SHOOT_SEC",
            # Short lead-in: less sprint-to-fight padding before first gunfire.
            os.environ.get("PUBG_OWNER_CLIP_PRE_SHOOT_SEC", "1.0"),
        )
    )


def clip_post_kill_sec() -> float:
    return float(
        os.environ.get(
            "PUBG_CLIP_POST_KILL_SEC",
            # Cut loot/run tails sooner after the kill.
            os.environ.get("PUBG_OWNER_POST_KILL_SEC", "3.0"),
        )
    )


def assemble_gun_pad_sec() -> float:
    """Padding before/after sustained gunfire when owner assembles 👍 singles."""
    return float(os.environ.get("PUBG_ASSEMBLE_GUN_PAD_SEC", "2.0"))


def fight_bounds(
    vod: Path,
    peak: float,
    file_dur: float | None = None,
) -> tuple[float, float]:
    from pubg_fight_segment import resolve_pubg_fight_bounds

    if file_dur is None:
        from shooter_vod_segment_feed import _ffprobe_duration

        file_dur = _ffprobe_duration(vod)
    start, dur, _report = resolve_pubg_fight_bounds(vod, peak, file_duration=file_dur)
    return float(start), float(start + dur)


def bounds_distinct(a: tuple[float, float], b: tuple[float, float]) -> bool:
    gap = min_fight_window_gap_sec()
    return a[1] + gap <= b[0] or b[1] + gap <= a[0]


def _ensure_payoff_in_clip(
    start: float,
    dur: float,
    peak: float,
    report: dict[str, Any],
) -> tuple[float, float]:
    """Expand/shift clip so kill notification stays inside after tighten."""
    kill = report.get("kill_sec") if report.get("kill_sec") is not None else report.get("kill_time")
    post = clip_post_kill_sec()
    end = start + dur
    if kill is not None:
        k = float(kill)
        if k + post > end:
            end = k + post
        if k < start:
            start = max(0.0, k - clip_pre_shoot_sec())
        dur = max(8.0, end - start)
    return float(start), float(dur)


def _gun_bin_active(row: dict[str, Any]) -> bool:
    gun_min = float(os.environ.get("PUBG_SEGMENT_GUN_ONSET_MIN", "0.025"))
    try:
        return float(row.get("gun", 0.0) or 0.0) >= gun_min
    except (TypeError, ValueError):
        return False


def clip_ends_on_gunfire(
    start: float,
    dur: float,
    report: dict[str, Any],
    *,
    tail_sec: float | None = None,
) -> bool:
    """True when the last seconds of the clip are still mid-burst."""
    timeline = report.get("timeline")
    if not isinstance(timeline, list) or not timeline:
        return False
    tail = float(tail_sec if tail_sec is not None else os.environ.get("PUBG_CLIP_END_GUN_TAIL_SEC", "2.5"))
    end = float(start) + float(dur)
    tail_start = end - tail
    for row in timeline:
        try:
            t = float(row["start"])
        except (KeyError, TypeError, ValueError):
            continue
        if t < tail_start - 0.5 or t > end + 1.0:
            continue
        # Prefer audible gun — silent DSP false-gun must not force extension,
        # but real loud tails (DGso@447) must not be truncated.
        if _bin_is_real_gunfight(row) or _gun_bin_active(row):
            # If rms is present and very low, treat as false-gun even if gun high.
            rms_raw = row.get("rms", row.get("audio_rms"))
            if rms_raw is not None:
                try:
                    if float(rms_raw) < float(os.environ.get("PUBG_CLIP_END_MIN_RMS", "0.015")):
                        continue
                except (TypeError, ValueError):
                    pass
            return True
    return False


def extend_end_past_active_gunfire(
    start: float,
    dur: float,
    report: dict[str, Any],
    *,
    max_dur: float,
    single: bool = False,
) -> tuple[float, float]:
    """Do not cut while the player is still shooting — extend to quiet (or max)."""
    timeline = report.get("timeline")
    if not isinstance(timeline, list) or not timeline:
        return float(start), float(dur)
    if not clip_ends_on_gunfire(start, dur, report):
        return float(start), float(dur)

    bin_sec = float(os.environ.get("PUBG_SEGMENT_BIN_SEC", "2"))
    quiet_need = float(os.environ.get("PUBG_CLIP_END_QUIET_SEC", "2.5"))
    start_f = float(start)
    end = start_f + float(dur)
    hard_cap = start_f + max(8.0, float(max_dur))
    fight_end = report.get("fight_end") or report.get("fight_end_sec")
    if fight_end is not None:
        hard_cap = min(hard_cap, float(fight_end) + quiet_need)

    rows = sorted(timeline, key=lambda r: float(r.get("start") or 0))
    new_end = end
    quiet = 0.0
    for row in rows:
        try:
            t = float(row["start"])
        except (KeyError, TypeError, ValueError):
            continue
        if t < end - bin_sec:
            continue
        if t > hard_cap:
            break
        if _bin_is_real_gunfight(row) or _gun_bin_active(row):
            # Skip silent false-gun when rms is known-low.
            rms_raw = row.get("rms", row.get("audio_rms"))
            if rms_raw is not None:
                try:
                    if float(rms_raw) < float(os.environ.get("PUBG_CLIP_END_MIN_RMS", "0.015")):
                        if t >= end - bin_sec:
                            quiet += bin_sec
                            new_end = max(new_end, min(hard_cap, t + 0.5))
                            if quiet >= quiet_need:
                                break
                        continue
                except (TypeError, ValueError):
                    pass
            quiet = 0.0
            new_end = max(new_end, min(hard_cap, t + bin_sec))
            continue
        if t >= end - bin_sec:
            quiet += bin_sec
            new_end = max(new_end, min(hard_cap, t + 0.5))
            if quiet >= quiet_need:
                break

    new_dur = max(float(dur), new_end - start_f)
    # If still ends hot at hard cap, shift window forward to land on quiet when possible.
    if clip_ends_on_gunfire(start_f, new_dur, report) and single:
        quiet_t = None
        quiet = 0.0
        for row in rows:
            try:
                t = float(row["start"])
            except (KeyError, TypeError, ValueError):
                continue
            if t < start_f:
                continue
            if _gun_bin_active(row):
                quiet = 0.0
                continue
            quiet += bin_sec
            if quiet >= quiet_need:
                quiet_t = t + 0.5
                break
        if quiet_t is not None and quiet_t > start_f + 8.0:
            new_end = min(hard_cap, quiet_t)
            new_start = max(0.0, new_end - float(max_dur))
            shoot = report.get("shooting_start")
            if shoot is not None:
                new_start = min(new_start, float(shoot))
            return float(new_start), float(max(8.0, new_end - new_start))
    return float(start_f), float(new_dur)




def _bin_is_real_gunfight(row: dict[str, Any]) -> bool:
    """True only for audible gunfire — silent high-gun bins are footsteps/false positives."""
    try:
        gun = float(row.get("gun", 0.0) or 0.0)
    except (TypeError, ValueError):
        return False
    gun_min = float(os.environ.get("PUBG_SEGMENT_GUN_ONSET_MIN", "0.025"))
    if gun < gun_min:
        return False
    rms_raw = row.get("rms", row.get("audio_rms"))
    rms_min = float(os.environ.get("PUBG_FIGHT_CLUSTER_MIN_RMS", "0.020"))
    # Missing rms must not count as a fight bin — silent DSP false-gun is how
    # loot_run clips (DGso 660/1026) snapped into "clusters" with gun>0, rms~0.
    if rms_raw is None:
        return os.environ.get("PUBG_FIGHT_CLUSTER_ALLOW_MISSING_RMS", "0") == "1"
    try:
        rms = float(rms_raw or 0.0)
    except (TypeError, ValueError):
        return False
    return rms >= rms_min


def snap_to_best_fight_cluster(
    start: float,
    dur: float,
    report: dict[str, Any],
    *,
    peak: float | None = None,
    max_cluster_sec: float | None = None,
) -> tuple[float, float]:
    """Keep one dense gunfight; drop bridged run between separate fights.

    #3hDKNrY4sGU_580 shipped 50s with two real bursts (~1–9s and ~28–35s) glued
    by quiet run. Owner wants only the hot burst (~5–8s). Pick the densest
    contiguous real-gun cluster (gun × rms) and pad slightly.
    """
    timeline = report.get("timeline")
    if not isinstance(timeline, list) or not timeline:
        return float(start), float(dur)
    max_cluster = float(
        max_cluster_sec
        if max_cluster_sec is not None
        # Owner: DGso@447 10s cut ended mid-burst (~448–460). 12s was too tight
        # for a single continuous exchange — allow ~18s before hard-cap.
        else os.environ.get("PUBG_FIGHT_CLUSTER_MAX_SEC", "18")
    )
    max_gap = float(os.environ.get("PUBG_FIGHT_CLUSTER_MAX_GAP_SEC", "2.5"))
    keep_pre = float(os.environ.get("PUBG_CLIP_PRE_SHOOT_SEC", "1.0"))
    keep_post = float(os.environ.get("PUBG_CLIP_POST_KILL_SEC", "2.0"))
    min_keep = float(os.environ.get("PUBG_FIGHT_CLUSTER_MIN_SEC", "5.0"))

    start_f = float(start)
    end_f = start_f + float(dur)
    rows: list[tuple[float, float, float]] = []
    for row in timeline:
        try:
            t = float(row["start"])
        except (KeyError, TypeError, ValueError):
            continue
        if t < start_f - 1.0 or t > end_f + 1.0:
            continue
        if not _bin_is_real_gunfight(row):
            continue
        gun = float(row.get("gun", 0.0) or 0.0)
        try:
            rms = float(row.get("rms", row.get("audio_rms", 0.05)) or 0.05)
        except (TypeError, ValueError):
            rms = 0.05
        rows.append((t, gun, rms))
    if not rows:
        return start_f, float(dur)

    rows.sort(key=lambda x: x[0])
    clusters: list[list[tuple[float, float, float]]] = [[rows[0]]]
    for item in rows[1:]:
        prev = clusters[-1][-1][0]
        if item[0] - prev <= max_gap:
            clusters[-1].append(item)
        else:
            clusters.append([item])

    def _peak_energy(cluster: list[tuple[float, float, float]]) -> float:
        return max(g * max(r, 0.01) for _, g, r in cluster)

    def _avg_energy(cluster: list[tuple[float, float, float]]) -> float:
        return sum(g * max(r, 0.01) for _, g, r in cluster) / max(1, len(cluster))

    peak_f = float(peak) if peak is not None else None
    best = None
    best_key = None
    for cluster in clusters:
        c_start = cluster[0][0]
        c_end = cluster[-1][0]
        peak_e = _peak_energy(cluster)
        avg_e = _avg_energy(cluster)
        # Peak proximity is only a weak tie-break — densest audible burst wins
        # (#3hDKNrY4sGU_580: early fight at peak lost to hotter 28–35s burst).
        near = 1 if (peak_f is not None and c_start - 1.0 <= peak_f <= c_end + 3.0) else 0
        key = (peak_e, avg_e, near, -(c_end - c_start))
        if best_key is None or key > best_key:
            best_key = key
            best = cluster
    assert best is not None
    c_start = best[0][0]
    c_end = best[-1][0]
    bin_sec = float(os.environ.get("PUBG_SEGMENT_BIN_SEC", "2"))
    new_start = max(start_f, c_start - keep_pre)
    new_end = min(end_f, c_end + bin_sec + keep_post)
    new_dur = max(min_keep, new_end - new_start)
    # Hard-cap only when multiple bursts were glued by run. A single continuous
    # fight may run long — leave length to extend/quiet-end + caller max_dur.
    if len(clusters) >= 2 and new_dur > max_cluster:
        hot_t = max(best, key=lambda x: x[1] * x[2])[0]
        if peak_f is not None and c_start - 1 <= peak_f <= c_end + 3:
            hot_t = peak_f
        new_start = max(start_f, hot_t - max_cluster * 0.35)
        new_end = min(end_f, new_start + max_cluster)
        new_start = max(start_f, new_end - max_cluster)
        new_dur = max(min_keep, new_end - new_start)
    # Only snap when we meaningfully shorten a bridged multi-fight window.
    if float(dur) - new_dur < 4.0 and new_start <= start_f + 1.0:
        return start_f, float(dur)
    return float(new_start), float(new_dur)


def trim_quiet_run_edges(
    start: float,
    dur: float,
    report: dict[str, Any],
    *,
    max_dur: float | None = None,
) -> tuple[float, float]:
    """Drop quiet run lead/tail so shipped clips are gunfight-only.

    Owner 👎 loot_run often came from min-duration padding and post-peak quiet,
    not from the fight core. Trim edges when timeline gun is cold.
    """
    timeline = report.get("timeline")
    if not isinstance(timeline, list) or not timeline:
        return float(start), float(dur)
    gun_min = float(os.environ.get("PUBG_SEGMENT_GUN_ONSET_MIN", "0.025"))
    edge = float(os.environ.get("PUBG_CLIP_TRIM_EDGE_SEC", "2.0"))
    keep_pre = float(os.environ.get("PUBG_CLIP_PRE_SHOOT_SEC", os.environ.get("PUBG_OWNER_PRE_SHOOT_SEC", "1.0")))
    keep_post = float(os.environ.get("PUBG_CLIP_POST_KILL_SEC", os.environ.get("PUBG_OWNER_POST_KILL_SEC", "3.5")))
    start_f = float(start)
    end_f = start_f + float(dur)
    rows = []
    for row in timeline:
        try:
            t = float(row["start"])
            g = float(row.get("gun", 0.0) or 0.0)
        except (KeyError, TypeError, ValueError):
            continue
        if start_f - 1.0 <= t <= end_f + 1.0:
            rows.append((t, g))
    if not rows:
        return start_f, float(dur)
    # Prefer audible gun rows when rms is present (drop silent false-gun run).
    hot = []
    for row in timeline:
        try:
            t = float(row["start"])
        except (KeyError, TypeError, ValueError):
            continue
        if start_f - 1.0 <= t <= end_f + 1.0 and _bin_is_real_gunfight(row):
            hot.append(t)
    if not hot:
        hot = [t for t, g in rows if g >= gun_min]
    if not hot:
        return start_f, float(dur)
    first_hot = min(hot)
    last_hot = max(hot)
    bin_sec = float(os.environ.get("PUBG_SEGMENT_BIN_SEC", "2"))
    new_start = max(start_f, first_hot - keep_pre)
    new_end = min(end_f, last_hot + bin_sec + keep_post)
    # Only trim when we actually remove cold edge (avoid no-op jitter).
    if new_start > start_f + 0.4:
        start_f = new_start
    if end_f - new_end > edge * 0.5:
        end_f = max(start_f + 5.0, new_end)
    new_dur = max(5.0, end_f - start_f)
    if max_dur is not None:
        new_dur = min(new_dur, float(max_dur))
    # Multi-fight windows: keep densest burst only (owner #3hDKNrY4sGU_580).
    start_f, new_dur = snap_to_best_fight_cluster(
        start_f, new_dur, report, max_cluster_sec=max_dur
    )
    if max_dur is not None:
        new_dur = min(new_dur, float(max_dur))
    return float(start_f), float(new_dur)


def tighten_pubg_clip_bounds(
    start: float,
    dur: float,
    report: dict[str, Any],
    *,
    peak: float | None = None,
    single: bool = False,
) -> tuple[float, float]:
    """Start at gunfire, end soon after kill — no loot-walk tail.

    Never end mid-burst: if the tail is still gunfire, extend through the fight.
    """
    from pubg_clip_shape_gate import (
        aggressive_tighten_for_shape,
        max_peak_position_frac,
        validate_clip_fight_shape,
    )

    # Collapse multi-burst windows BEFORE shoot/kill anchors lock onto the wrong burst
    # (#3hDKNrY4sGU_580: shoot_start≈early fight hid the hotter 28–35s cluster).
    if float(dur) >= 16.0 and isinstance(report.get("timeline"), list):
        start, dur = snap_to_best_fight_cluster(
            float(start),
            float(dur),
            report,
            peak=peak,
            max_cluster_sec=min(
                float(os.environ.get("PUBG_SINGLE_MAX_SEC", "90") if single else os.environ.get("PUBG_SEGMENT_MAX_SEC", "55")),
                float(os.environ.get("PUBG_FIGHT_CLUSTER_MAX_SEC", "18")),
            ),
        )

    pre_pad = clip_pre_shoot_sec()
    post_kill = clip_post_kill_sec()
    max_lead = float(os.environ.get("PUBG_CLIP_MAX_PRE_SHOOT_SEC", "1.2"))
    min_dur = float(os.environ.get("PUBG_CLIP_MIN_TIGHTEN_SEC", "8"))
    if single:
        min_dur = max(min_dur, float(os.environ.get("PUBG_SINGLE_MIN_SEC", "8")))
    max_dur = float(
        os.environ.get("PUBG_SINGLE_MAX_SEC", "90")
        if single
        else os.environ.get("PUBG_SEGMENT_MAX_SEC", "55")
    )

    shoot = report.get("shooting_start")
    # Do not jump back to an earlier shoot_start outside the snapped fight cluster
    # (#3hDKNrY4sGU_580: snap kept 29–36s, then shoot_start=1s reopened the run pad).
    if shoot is not None:
        shoot_f = float(shoot)
        if float(start) - 2.0 <= shoot_f <= float(start) + float(dur) + 2.0:
            start = shoot_f - min(pre_pad, max_lead)
    kill = report.get("kill_sec") if report.get("kill_sec") is not None else report.get("kill_time")
    fight_end = report.get("fight_end") or report.get("fight_end_sec")
    end = float(start) + float(dur)

    # Montage: prefer end soon after kill unless gunfire continues hard after kill.
    gun_continues_after_kill = False
    if kill is not None and isinstance(report.get("timeline"), list):
        for row in report["timeline"]:
            try:
                t = float(row["start"])
            except (KeyError, TypeError, ValueError):
                continue
            if t < float(kill) + 1.0:
                continue
            if t > float(kill) + 8.0:
                break
            if _gun_bin_active(row):
                gun_continues_after_kill = True
                break

    if kill is not None and not single and not gun_continues_after_kill:
        end = min(end, float(kill) + post_kill)
    if fight_end is not None:
        end = min(end, float(fight_end))
    raw_dur = max(0.0, end - float(start))
    kill_end = float(kill) + post_kill if kill is not None else None
    long_loot_tail = (
        kill is not None
        and not single
        and not gun_continues_after_kill
        and fight_end is not None
        and kill_end is not None
        and float(fight_end) > kill_end + 2.0
    )
    if long_loot_tail and kill_end is not None:
        dur = min(raw_dur, max(0.0, kill_end - float(start)))
    elif (
        single
        and kill is not None
        and not gun_continues_after_kill
        and kill_end is not None
        and 0.0 < (kill_end - float(start)) < min_dur
    ):
        # Short real kill exchange — do not invent loot/run pad to hit 18–20s.
        dur = max(0.0, kill_end - float(start))
    else:
        dur = max(min_dur, raw_dur)

    if peak is not None:
        ok, reason = validate_clip_fight_shape(start, dur, float(peak), report)
        if not ok and "fight_at_end" in reason and shoot is not None:
            max_frac = max_peak_position_frac()
            need_span = (float(peak) - float(shoot) + max_lead) / max(max_frac, 0.05)
            start = max(0.0, float(shoot) - max_lead)
            if long_loot_tail and kill_end is not None:
                end = float(kill_end)
                dur = max(0.0, end - start)
            else:
                end = max(end, start + max(min_dur, need_span))
                if fight_end is not None:
                    end = min(end, float(fight_end))
                dur = max(min_dur, end - start)
            ok, reason = validate_clip_fight_shape(start, dur, float(peak), report)
        if not ok:
            alt_start, alt_dur = aggressive_tighten_for_shape(
                start, dur, float(peak), report, single=single
            )
            if long_loot_tail and kill_end is not None:
                alt_end = min(alt_start + alt_dur, float(kill_end))
                alt_dur = max(0.0, alt_end - alt_start)
            min_alt = 0.0 if long_loot_tail else min_dur
            if alt_dur >= min_alt:
                alt_ok, _alt_reason = validate_clip_fight_shape(
                    alt_start, alt_dur, float(peak), report
                )
                if alt_ok:
                    start, dur = alt_start, alt_dur
                    ok = True
        if ok:
            start, dur = _ensure_payoff_in_clip(start, dur, float(peak), report)
            if not long_loot_tail:
                # Keep short single kill spans tight after payoff rebalance.
                if not (
                    single
                    and kill is not None
                    and not gun_continues_after_kill
                    and kill_end is not None
                    and dur < min_dur
                ):
                    dur = max(min_dur, dur)

    start, dur = extend_end_past_active_gunfire(
        start, dur, report, max_dur=max_dur, single=single
    )
    start, dur = trim_quiet_run_edges(start, dur, report, max_dur=max_dur)
    start, dur = snap_to_best_fight_cluster(
        start, dur, report, peak=peak, max_cluster_sec=min(float(max_dur), 14.0)
    )
    # Short real fights stay short — do not re-inflate to legacy 18–20s.
    dur = min(float(dur), float(max_dur))
    return float(start), float(dur)


def _gunfire_end_from_report(report: dict[str, Any], *, fallback: float) -> float:
    timeline = report.get("timeline") or []
    gun_times: list[float] = []
    gun_min = float(os.environ.get("PUBG_SEGMENT_GUN_ONSET_MIN", "0.025"))
    for row in timeline:
        try:
            # Gun-only — loud loot/run score must not invent fight_end.
            if float(row.get("gun", 0.0)) >= gun_min:
                gun_times.append(float(row["start"]))
        except (TypeError, ValueError, KeyError):
            continue
    if gun_times:
        sample = float(os.environ.get("PUBG_SEGMENT_BIN_SEC", "2"))
        return max(gun_times) + sample
    for key in ("fight_end", "fight_end_sec"):
        if report.get(key) is not None:
            return float(report[key])
    return float(fallback)


def _assemble_gun_bins(
    report: dict[str, Any],
    *,
    min_gun: float = 0.020,
) -> list[float]:
    times: list[float] = []
    for row in report.get("timeline") or []:
        try:
            gun = float(row.get("gun", 0.0) or 0.0)
            # Gun-only assemble core — score-as-gun padded loot/run into 👍 parts.
            if gun >= min_gun:
                times.append(float(row["start"]))
        except (TypeError, ValueError, KeyError):
            continue
    return times


def assemble_max_part_sec() -> float:
    """Hard cap for one 👍 part in owner montage — long windows = loot/run padding."""
    return max(14.0, float(os.environ.get("PUBG_ASSEMBLE_MAX_SEC", "28")))


def tighten_pubg_assemble_bounds(
    start: float,
    dur: float,
    report: dict[str, Any],
    *,
    peak: float,
    file_dur: float,
    owner_start: float | None = None,
    owner_dur: float | None = None,
) -> tuple[float, float]:
    """Re-trim 👍 singles for montage: drop loot-walk, keep gunfire ± pad, hard-cap length.

    Tovruh 5266 shipped ~38–60s of quiet run because shooting_start/fight_end came from a
    zero-gun timeline and bounds_locked skipped the montage part ceiling. Prefer real gun
    bins; if none, fall back to the owner's labeled window or a tight peak pocket.
    """
    from pubg_clip_shape_gate import aggressive_tighten_for_shape, validate_clip_fight_shape

    pad = assemble_gun_pad_sec()
    min_dur = max(8.0, float(os.environ.get("PUBG_ASSEMBLE_MIN_SEC", "10")))
    max_dur = assemble_max_part_sec()
    peak_f = float(peak)
    file_d = float(file_dur)

    gun_bins = _assemble_gun_bins(report)
    kill = report.get("kill_sec")
    if kill is None:
        kill = report.get("kill_time")
    kill_f = float(kill) if kill is not None else None

    if gun_bins:
        # Drop leading/trailing silence: only first→last audible gun + pad.
        core_start = min(gun_bins)
        core_end = max(gun_bins) + float(os.environ.get("PUBG_SEGMENT_BIN_SEC", "2"))
        if kill_f is not None:
            core_end = max(core_end, kill_f + clip_post_kill_sec())
        start = max(0.0, core_start - pad)
        end = min(file_d, core_end + pad)
    elif owner_start is not None and owner_dur is not None and float(owner_dur) >= min_dur:
        # Segmenter lied (all-zero timeline) — keep the window the owner actually 👍'd.
        start = max(0.0, float(owner_start))
        end = min(file_d, start + min(float(owner_dur), max_dur))
        dur = max(min_dur, end - start)
        log.info(
            "assemble tighten peak=%.1f: no gun bins — use owner window %.1f+%.1f",
            peak_f,
            start,
            dur,
        )
        return float(start), float(min(dur, max_dur))
    else:
        # Quiet fallback: short pocket around peak, not a 55s resolve window of running.
        pre = float(os.environ.get("PUBG_ASSEMBLE_QUIET_PRE_SEC", "6"))
        post = float(os.environ.get("PUBG_ASSEMBLE_QUIET_POST_SEC", "12"))
        start = max(0.0, peak_f - pre)
        end = min(file_d, peak_f + post)
        log.info(
            "assemble tighten peak=%.1f: no gun bins / no owner window — tight pocket %.1f→%.1f",
            peak_f,
            start,
            end,
        )

    dur = max(min_dur, end - start)

    # Cap long fights: keep the gun-dense / kill side of the window, drop run-in/loot.
    if dur > max_dur:
        end = start + dur
        # Prefer ending on kill+post when present; else keep the last max_dur ending at core_end.
        if kill_f is not None and start <= kill_f <= end:
            end = min(file_d, max(kill_f + clip_post_kill_sec(), peak_f + pad))
            start = max(0.0, end - max_dur)
            if start > peak_f - 3.0:
                start = max(0.0, peak_f - 3.0)
                end = min(file_d, start + max_dur)
        else:
            # Slide window to cover peak, bias toward the end of gunfire (payoff).
            end = min(file_d, max(end, peak_f + pad))
            start = max(0.0, end - max_dur)
            if peak_f < start:
                start = max(0.0, peak_f - pad)
                end = min(file_d, start + max_dur)
        dur = max(min_dur, end - start)
        dur = min(dur, max_dur)

    ok, reason = validate_clip_fight_shape(start, dur, peak_f, report)
    if not ok:
        alt_start, alt_dur = aggressive_tighten_for_shape(start, dur, peak_f, report)
        alt_dur = min(float(alt_dur), max_dur)
        if alt_dur >= min_dur:
            alt_ok, _ = validate_clip_fight_shape(alt_start, alt_dur, peak_f, report)
            if alt_ok:
                start, dur = alt_start, alt_dur
                ok = True
        if not ok:
            log.warning("assemble tighten shape reject peak=%.1f: %s", peak_f, reason)
    return float(start), float(min(dur, max_dur))


def pubg_clip_has_gunfire(
    vod: Path,
    start: float,
    dur: float,
    peak: float,
    *,
    single: bool = False,
) -> tuple[bool, str]:
    """Reject running/menu clips — require audible gunfire in the fight core."""
    from gameplay_gate import score_pubg_gunfire_audio

    min_gun = float(os.environ.get("PUBG_CLIP_MIN_GUN_DENSITY", "0.055"))
    if single:
        min_gun = float(os.environ.get("PUBG_SINGLE_MIN_GUN_DENSITY", str(min_gun)))
    min_burst = float(os.environ.get("PUBG_CLIP_MIN_BURST_RATIO", "4.8"))

    gun, burst, _rms = score_pubg_gunfire_audio(vod, start, dur)
    if gun >= min_gun and burst >= min_burst:
        return True, "gun_ok"

    core_start = max(start, float(peak) - 10.0)
    core_dur = min(float(dur), max(8.0, float(peak) - core_start + 8.0))
    gun_core, burst_core, _ = score_pubg_gunfire_audio(vod, core_start, core_dur)
    if gun_core >= min_gun and burst_core >= min_burst:
        return True, "gun_core_ok"

    return False, f"low_gun whole={gun:.3f} core={gun_core:.3f} burst={burst:.2f} need>={min_gun:.3f}/{min_burst:.1f}"


def peak_fight_report(
    vod: Path,
    peak: float,
    file_dur: float | None = None,
) -> dict[str, Any]:
    from pubg_fight_segment import resolve_pubg_fight_bounds

    if file_dur is None:
        from shooter_vod_segment_feed import _ffprobe_duration

        file_dur = _ffprobe_duration(vod)
    _start, _dur, report = resolve_pubg_fight_bounds(vod, peak, file_duration=file_dur)
    return report


def peak_has_kill(
    vod: Path,
    peak: float,
    file_dur: float | None = None,
) -> bool:
    report = peak_fight_report(vod, peak, file_dur)
    if report.get("kill_sec") is not None or report.get("kill_time") is not None:
        return True
    return float(report.get("killfeed_score", 0.0) or 0.0) >= 0.35


def dedupe_peaks_by_fight_window(
    vod: Path,
    peaks: list[float],
    *,
    file_dur: float | None = None,
) -> list[float]:
    kept: list[float] = []
    bounds: list[tuple[float, float]] = []
    for peak in sorted(peaks):
        window = fight_bounds(vod, peak, file_dur)
        if any(not bounds_distinct(window, prev) for prev in bounds):
            continue
        kept.append(float(peak))
        bounds.append(window)
    return kept


def peak_blocked_by_used_fights(
    vod: Path,
    peak: float,
    used_peaks: list[float],
    *,
    file_dur: float | None = None,
    peak_gap_sec: float = 0.0,
) -> bool:
    """True when peak overlaps a used fight window or is within peak_gap_sec."""
    from vod_peak_gap import peak_too_close

    if peak_too_close(peak, used_peaks, peak_gap_sec):
        return True
    if not used_peaks:
        return False
    window = fight_bounds(vod, peak, file_dur)
    for used in used_peaks:
        if not bounds_distinct(window, fight_bounds(vod, used, file_dur)):
            return True
    return False


def _row_peak(row: dict[str, Any]) -> float:
    return float(row.get("peak_start", row.get("start", 0)) or 0)


def filter_rows_distinct_fights(
    vod: Path,
    rows: list[dict[str, Any]],
    *,
    file_dur: float | None = None,
    max_clips: int | None = None,
) -> list[dict[str, Any]]:
    """Drop montage rows whose trimmed fight windows overlap."""
    if not rows:
        return []
    ranked = sorted(rows, key=lambda row: float(row.get("score", 0) or 0), reverse=True)
    kept: list[dict[str, Any]] = []
    bounds: list[tuple[float, float]] = []
    for row in ranked:
        peak = _row_peak(row)
        window = fight_bounds(vod, peak, file_dur)
        if any(not bounds_distinct(window, prev) for prev in bounds):
            continue
        kept.append(row)
        bounds.append(window)
        if max_clips is not None and len(kept) >= max_clips:
            break
    kept.sort(key=_row_peak)
    return kept


def peak_shape_ok(
    vod: Path,
    peak: float,
    *,
    file_dur: float | None = None,
) -> bool:
    from pubg_clip_shape_gate import validate_clip_fight_shape
    from pubg_fight_segment import resolve_pubg_fight_bounds

    if file_dur is None:
        from shooter_vod_segment_feed import _ffprobe_duration

        file_dur = _ffprobe_duration(vod)
    start, dur, report = resolve_pubg_fight_bounds(vod, peak, file_duration=file_dur)
    start, dur = tighten_pubg_clip_bounds(start, dur, report, peak=float(peak))
    ok, _reason = validate_clip_fight_shape(start, dur, float(peak), report)
    return ok


def peak_presend_ok(
    vod: Path,
    peak: float,
    *,
    file_dur: float | None = None,
) -> tuple[bool, str, float, float]:
    """Full presend check on tightened fight clip — not just shape."""
    from pubg_fight_segment import resolve_pubg_fight_bounds
    from pubg_quality_score import score_pubg_window

    if file_dur is None:
        from shooter_vod_segment_feed import _ffprobe_duration

        file_dur = _ffprobe_duration(vod)
    start, dur, report = resolve_pubg_fight_bounds(vod, peak, file_duration=file_dur)
    start, dur = tighten_pubg_clip_bounds(start, dur, report, peak=float(peak))
    dur = max(10.0, float(dur))
    ok, reason, _rep = score_pubg_window(vod, start, dur)
    return ok, reason, float(start), float(dur)


def select_distinct_kill_peaks(
    vod: Path,
    pool: list[float],
    *,
    min_clips: int = 2,
    max_clips: int = 2,
    file_dur: float | None = None,
    avoid: list[float] | None = None,
) -> list[float]:
    """Pick distinct fights with kill notification — gunfire first, payoff confirmed."""
    from pubg_fast_peak_rank import rank_peaks_fast

    avoid = avoid or []
    filtered = [
        float(p)
        for p in pool
        if not any(abs(float(p) - float(bad)) <= 25.0 for bad in avoid)
    ]
    if len(filtered) < min_clips:
        return []
    ranked, _reason, meta = rank_peaks_fast(
        vod,
        filtered,
        "pubg",
        part_sec=14.0,
        max_probes=max(len(filtered), min_clips * 8),
    )
    presend_hits: list[tuple[float, float]] = []
    for p in ranked:
        ok, _reason, _start, _dur = peak_presend_ok(vod, float(p), file_dur=file_dur)
        if ok:
            note = float(meta.get(float(p), {}).get("notification_score", 0.0) or 0.0)
            presend_hits.append((float(p), note))
    presend_hits.sort(key=lambda item: -item[1])
    with_hit = [p for p, _note in presend_hits]
    if len(with_hit) < min_clips:
        with_hit = [
            float(p)
            for p in ranked
            if meta.get(float(p), {}).get("notification_hit")
            and peak_shape_ok(vod, float(p), file_dur=file_dur)
        ]
    if len(with_hit) < min_clips:
        shaped = [float(p) for p in ranked if peak_shape_ok(vod, float(p), file_dur=file_dur)]
        with_hit = shaped or with_hit
    candidates = with_hit or ranked
    kept: list[float] = []
    bounds: list[tuple[float, float]] = []
    for peak in candidates:
        window = fight_bounds(vod, peak, file_dur)
        if any(not bounds_distinct(window, prev) for prev in bounds):
            continue
        kept.append(float(peak))
        bounds.append(window)
        if len(kept) >= max_clips:
            break
    return kept


__all__ = [
    "bounds_distinct",
    "clip_post_kill_sec",
    "clip_pre_shoot_sec",
    "dedupe_peaks_by_fight_window",
    "fight_bounds",
    "filter_rows_distinct_fights",
    "min_fight_window_gap_sec",
    "peak_blocked_by_used_fights",
    "peak_fight_report",
    "peak_has_kill",
    "peak_presend_ok",
    "peak_shape_ok",
    "select_distinct_kill_peaks",
    "tighten_pubg_clip_bounds",
    "snap_to_best_fight_cluster",
]
