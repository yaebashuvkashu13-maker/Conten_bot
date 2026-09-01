#!/usr/bin/env python3
"""Rebuild + send PUBG montages for owner-approved VODs (sequential, trimmed fights)."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("pubg_owner_redo_approved")

# Latest owner-reviewed VODs from feedback thread.
DEFAULT_VODS = (
    "Tovruh33adY",
    "bMn-6uTsDBg",
    "Z7wR4vZkn5E",
)


def merge_seed_labels(profile: str = "pubg") -> None:
    from runtime_labels import ensure_runtime_labels, load_runtime_labels, save_runtime_labels, seed_labels_path

    ensure_runtime_labels(profile)
    data = load_runtime_labels(profile)
    seed_path = seed_labels_path(profile)
    if not seed_path.is_file():
        return
    seed = json.loads(seed_path.read_text(encoding="utf-8"))
    videos = data.setdefault("videos", {})
    for vid, rows in (seed.get("videos") or {}).items():
        bucket = videos.setdefault(vid, [])
        seen = {(r.get("time_sec"), r.get("label"), r.get("role")) for r in bucket}
        for row in rows:
            key = (row.get("time_sec"), row.get("label"), row.get("role"))
            if key not in seen:
                bucket.append(dict(row))
                seen.add(key)
    save_runtime_labels(profile, data)


def _resolve_vod(vid: str) -> Path | None:
    name = vid if vid.endswith(".mp4") else f"yt_{vid}.mp4"
    for base in (
        Path("/root/data/pubg/youtube_nightly/inbox"),
        Path("/root/data/mlbb/youtube_nightly/inbox"),
    ):
        candidate = base / name
        if candidate.is_file():
            return candidate
    return None


def _owner_good_peaks(vod: Path) -> list[float]:
    from pubg_owner_calibration import labels_for_video
    from pubg_owner_style import style_avoid_peaks

    peaks: list[float] = []
    for row in labels_for_video(vod):
        if str(row.get("label") or "") != "good":
            continue
        if str(row.get("role") or "").lower() == "anti_style":
            continue
        try:
            peaks.append(float(row["time_sec"]))
        except (KeyError, TypeError, ValueError):
            continue
    peaks.sort()
    avoid = style_avoid_peaks(vod)
    out: list[float] = []
    for peak in peaks:
        if any(abs(peak - float(bad)) <= 25.0 for bad in avoid):
            continue
        if any(abs(peak - p) <= 8.0 for p in out):
            continue
        out.append(peak)
    return out


def _probe_anchor_vicinity(
    vod: Path,
    anchor: float,
    *,
    radius: float = 100.0,
    step: float = 12.0,
) -> list[float]:
    """Lightweight fight probe around owner anchor when dense pool missed the zone."""
    from pubg_fast_peak_rank import score_peak_fast

    hits: list[tuple[float, float]] = []
    t = float(anchor) - radius
    end_t = float(anchor) + radius
    while t <= end_t:
        row = score_peak_fast(vod, t, part_sec=14.0, profile="pubg")
        score = float(row.get("fast_score", 0.0) or 0.0)
        if row.get("loot_walk"):
            t += step
            continue
        if score >= float(os.environ.get("PUBG_OWNER_VICINITY_MIN_SCORE", "0.26")):
            hits.append((score, float(t)))
        t += step
    hits.sort(key=lambda item: (-item[0], abs(item[1] - anchor)))
    out: list[float] = []
    for _score, peak in hits:
        if any(abs(peak - p) <= 10.0 for p in out):
            continue
        out.append(peak)
        if len(out) >= 8:
            break
    return out


def _min_window_gap_sec() -> float:
    return float(os.environ.get("PUBG_OWNER_REDO_MIN_WINDOW_GAP_SEC", "30"))


def _fight_bounds(vod: Path, peak: float, file_dur: float | None = None) -> tuple[float, float]:
    from pubg_fight_segment import resolve_pubg_fight_bounds
    from shooter_vod_segment_feed import _ffprobe_duration

    if file_dur is None:
        file_dur = _ffprobe_duration(vod)
    start, dur, _report = resolve_pubg_fight_bounds(vod, peak, file_duration=file_dur)
    return float(start), float(start + dur)


def _bounds_distinct(a: tuple[float, float], b: tuple[float, float]) -> bool:
    gap = _min_window_gap_sec()
    return a[1] + gap <= b[0] or b[1] + gap <= a[0]


def _dedupe_peak_windows(
    vod: Path,
    peaks: list[float],
    *,
    file_dur: float | None = None,
) -> list[float]:
    """Drop peaks whose trimmed fight windows overlap — keep earlier / higher owner order."""
    kept: list[float] = []
    bounds: list[tuple[float, float]] = []
    for peak in sorted(peaks):
        window = _fight_bounds(vod, peak, file_dur)
        if any(not _bounds_distinct(window, prev) for prev in bounds):
            continue
        kept.append(float(peak))
        bounds.append(window)
    return kept


def _full_vod_peak_pool(vod: Path, *, min_clips: int) -> tuple[list[float], str]:
    from shooter_vod_fast_scan import discover_montage_gun_peaks

    gap = float(os.environ.get("SHOOTER_VOD_MONTAGE_POOL_PART_GAP_SEC", "45"))
    return discover_montage_gun_peaks(vod, "pubg", min_clips=min_clips, gap_sec=gap)


def _peak_fight_report(vod: Path, peak: float, file_dur: float | None = None) -> dict:
    from pubg_fight_segment import resolve_pubg_fight_bounds
    from shooter_vod_segment_feed import _ffprobe_duration

    if file_dur is None:
        file_dur = _ffprobe_duration(vod)
    _start, _dur, report = resolve_pubg_fight_bounds(vod, peak, file_duration=file_dur)
    return report


def _peak_has_kill(vod: Path, peak: float, file_dur: float | None = None) -> bool:
    report = _peak_fight_report(vod, peak, file_dur)
    if report.get("kill_sec") is not None or report.get("kill_time") is not None:
        return True
    return float(report.get("killfeed_score", 0.0) or 0.0) >= 0.35


def _tighten_owner_clip_bounds(
    start: float,
    dur: float,
    report: dict,
) -> tuple[float, float]:
    """Owner redo: start at gunfire, end soon after kill — no loot walk tail."""
    pre_pad = float(os.environ.get("PUBG_OWNER_CLIP_PRE_SHOOT_SEC", "1.5"))
    post_kill = float(os.environ.get("PUBG_OWNER_POST_KILL_SEC", "5.0"))
    shoot = report.get("shooting_start")
    if shoot is not None:
        start = float(shoot) - pre_pad
    kill = report.get("kill_sec") if report.get("kill_sec") is not None else report.get("kill_time")
    end = float(start) + float(dur)
    if kill is not None:
        end = min(end, float(kill) + post_kill)
    fight_end = report.get("fight_end")
    if fight_end is not None:
        end = min(end, float(fight_end))
    dur = max(10.0, end - float(start))
    return float(start), float(dur)


def _pick_pair_second(
    vod: Path,
    candidates: list[float],
    anchor: float,
    *,
    file_dur: float | None = None,
) -> float:
    """Next distinct fight in timeline; prefer first with a kill."""
    after = sorted(p for p in candidates if p > float(anchor) + 8.0)
    before = sorted((p for p in candidates if p < float(anchor) - 8.0), reverse=True)

    def _choose(pool: list[float]) -> float | None:
        if not pool:
            return None
        with_kill = [p for p in pool if _peak_has_kill(vod, p, file_dur)]
        return float((with_kill or pool)[0])

    prefer_after = os.environ.get("PUBG_OWNER_REDO_PREFER_AFTER_ANCHOR", "1") == "1"
    if prefer_after:
        picked = _choose(after) or _choose(before)
    else:
        picked = _choose(before) or _choose(after)
    return picked if picked is not None else float(sorted(candidates)[0])


def _pair_distinct_peaks(
    vod: Path,
    anchor: float,
    *,
    min_clips: int,
    max_clips: int,
    file_dur: float | None = None,
) -> list[float]:
    """Anchor fight + one more distinct fight from the VOD (not the same engagement)."""
    from pubg_owner_style import rank_peaks_by_style, style_avoid_peaks
    from shooter_vod_segment_feed import _ffprobe_duration

    if file_dur is None:
        file_dur = _ffprobe_duration(vod)
    avoid = style_avoid_peaks(vod)
    pool, pool_reason = _full_vod_peak_pool(vod, min_clips=min_clips)
    anchor_bounds = _fight_bounds(vod, anchor, file_dur)
    candidates: list[float] = []
    for peak in pool:
        p = float(peak)
        if any(abs(p - float(bad)) <= 25.0 for bad in avoid):
            continue
        if abs(p - float(anchor)) <= 4.0:
            continue
        if not _bounds_distinct(anchor_bounds, _fight_bounds(vod, p, file_dur)):
            continue
        candidates.append(p)
    if not candidates:
        log.warning("pair distinct: no second fight for anchor=%.0f vod=%s", anchor, vod.name)
        return [float(anchor)]

    ranked, style_reason, _sims = rank_peaks_by_style(vod, candidates, part_sec=14.0)
    second = _pick_pair_second(vod, candidates, anchor, file_dur=file_dur)

    peaks = _dedupe_peak_windows(vod, [float(anchor), second], file_dur=file_dur)
    log.info(
        "pair vod=%s anchor=%.0f peaks=%s pool=%s %s",
        vod.name,
        anchor,
        peaks,
        pool_reason,
        style_reason,
    )
    return peaks[:max_clips]


def _discover_cluster_peaks(
    vod: Path,
    anchor: float,
    *,
    min_clips: int,
    max_clips: int,
) -> list[float]:
    from pubg_owner_style import rank_peaks_by_style, style_avoid_peaks
    from shooter_vod_fast_scan import discover_montage_gun_peaks
    from vod_montage_cluster import montage_cluster_span_sec, pick_montage_rows

    span = float(os.environ.get("PUBG_OWNER_CLUSTER_SPAN_SEC", "120"))
    gap = float(os.environ.get("SHOOTER_VOD_MONTAGE_PART_GAP_SEC", "20"))
    pool, _reason = discover_montage_gun_peaks(
        vod,
        "pubg",
        min_clips=min_clips,
        gap_sec=gap,
    )
    avoid = style_avoid_peaks(vod)
    near = [
        float(p)
        for p in pool
        if abs(float(p) - float(anchor)) <= span
        and not any(abs(float(p) - float(bad)) <= 25.0 for bad in avoid)
    ]
    near.extend(_probe_anchor_vicinity(vod, anchor, radius=span))
    if not any(abs(float(p) - float(anchor)) <= 4.0 for p in near):
        near.append(float(anchor))
    ranked, _style_reason, sims = rank_peaks_by_style(vod, sorted(set(near)), part_sec=14.0)
    rows = [
        {
            "segment_id": f"redo_{vod.stem}_{int(round(p))}",
            "peak_start": float(p),
            "start": float(p),
            "score": 0.35 + 0.65 * float(sims.get(float(p), 0.5)),
            "style_sim": sims.get(float(p)),
            "owner_anchor": True,
        }
        for p in ranked
    ]
    os.environ["SHOOTER_VOD_MONTAGE_CLUSTER_SPAN_SEC"] = str(int(max(span, 90)))
    picked = pick_montage_rows(
        rows,
        min_clips=min_clips,
        max_clips=max_clips,
        gap_sec=gap,
        anchor_peaks=[float(anchor)],
    )
    peaks = sorted(float(r["peak_start"]) for r in picked[:max_clips])
    if float(anchor) not in peaks and len(peaks) < max_clips:
        peaks = sorted(set(peaks + [float(anchor)]))
    if len(peaks) < min_clips:
        extras = [float(p) for p in ranked if float(p) not in peaks]
        for peak in extras:
            if any(abs(peak - p) <= gap for p in peaks):
                continue
            peaks.append(peak)
            peaks.sort()
            if len(peaks) >= min_clips:
                break
    log.info("cluster vod=%s anchor=%.0f peaks=%s (%s)", vod.name, anchor, peaks, _style_reason)
    return peaks[:max_clips]


def resolve_montage_peaks(vod: Path, *, min_clips: int = 2, max_clips: int = 2) -> list[float]:
    from pubg_owner_style import style_reference_peaks
    from shooter_vod_segment_feed import _ffprobe_duration

    file_dur = _ffprobe_duration(vod)
    owner = _owner_good_peaks(vod)
    if len(owner) >= min_clips:
        peaks = _dedupe_peak_windows(vod, owner, file_dur=file_dur)
        if len(peaks) >= min_clips:
            return peaks[:max_clips]

    refs = style_reference_peaks(vod) or owner
    if not refs:
        return []
    anchor = float(refs[0])
    paired = _pair_distinct_peaks(
        vod,
        anchor,
        min_clips=min_clips,
        max_clips=max_clips,
        file_dur=file_dur,
    )
    if len(paired) >= min_clips:
        return paired

    cluster = _discover_cluster_peaks(vod, anchor, min_clips=min_clips, max_clips=max_clips)
    cluster = _dedupe_peak_windows(vod, cluster, file_dur=file_dur)
    if len(cluster) >= min_clips:
        return cluster[:max_clips]
    merged = _dedupe_peak_windows(vod, sorted(set(owner + cluster)), file_dur=file_dur)
    return merged[:max_clips] if len(merged) >= min_clips else merged


def clear_vod_sent(vod: Path, game: str = "pubg") -> int:
    from shooter_vod_segment_store import load_feed_sent, mark_feed_sent

    vid = vod.stem[3:] if vod.stem.startswith("yt_") else vod.stem
    sent = load_feed_sent(game)
    drop = {sid for sid in sent if sid.startswith(f"{vid}_")}
    if not drop:
        return 0
    kept = sent - drop
    mark_feed_sent(game, list(kept))
    log.info("cleared sent vod=%s n=%s", vid, len(drop))
    return len(drop)


def _load_video_bot_env() -> None:
    from vod_env import load_env

    for key, val in load_env().items():
        os.environ.setdefault(key, val)


def _apply_redo_env() -> None:
    overrides = {
        "DAILY_GAME_CYCLE_ENABLED": "0",
        "PUBG_FIGHT_SEGMENTER": "1",
        "PUBG_OWNER_REDO": "1",
        "SHOOTER_VOD_MONTAGE_SEQUENTIAL": "1",
        "SHOOTER_VOD_MONTAGE_MIN_CLIPS": "2",
        "SHOOTER_VOD_MONTAGE_MAX_CLIPS": "2",
        "SHOOTER_VOD_MONTAGE_PREFER_PARTS": "2",
        "SHOOTER_VOD_MONTAGE_CLUSTER_SPAN_SEC": "360",
        "PUBG_OWNER_CLUSTER_SPAN_SEC": "120",
        "SHOOTER_VOD_MONTAGE_PART_GAP_SEC": "55",
        "SHOOTER_VOD_MONTAGE_POOL_PART_GAP_SEC": "45",
        "PUBG_OWNER_REDO_MIN_WINDOW_GAP_SEC": "30",
        "PUBG_SEGMENT_SCAN_AFTER": "40",
        "PUBG_OWNER_CLIP_PRE_SHOOT_SEC": "1.5",
        "PUBG_OWNER_POST_KILL_SEC": "5.0",
        "PUBG_SEGMENT_MAX_PREFLIGHT_SEC": "4",
        "PUBG_REJECT_LOOT_WALK": "0",
        "PUBG_EARLY_PAYOFF_REJECT": "0",
        "VOD_PRESEND_CACHE": "0",
        "PUBG_VOD_MONTAGE_MIN_FINAL_SEC": "30",
        "PUBG_STYLE_RANK_BLEND": "0.58",
    }
    for key, val in overrides.items():
        os.environ[key] = val


def _redo_chat_id() -> str:
    """Owner-approved redo goes to the owner, not the PUBG colleague queue."""
    explicit = os.environ.get("PUBG_OWNER_REDO_CHAT_ID", "").strip()
    if explicit:
        return explicit
    owner = os.environ.get("TG_CHAT_ID", "").strip()
    if owner:
        return owner
    return os.environ.get("PUBG_CHAT_IDS", "").split(",")[0].strip()


def redo_vod(vid: str, *, dry_run: bool = False, send: bool = True) -> dict:
    from pubg_fight_segment import clear_segment_cache, resolve_pubg_fight_bounds
    from pubg_owner_peak_montage import _peak_rows, file_sha256
    from shooter_vod_segment_feed import _ffprobe_duration, _send_montage

    vod = _resolve_vod(vid)
    if vod is None:
        return {"vid": vid, "status": "missing_vod"}

    clear_segment_cache()
    try:
        from vod_presend_cache import clear_presend_cache

        clear_presend_cache()
    except Exception:
        pass
    peaks = resolve_montage_peaks(vod)
    if len(peaks) < 2:
        return {"vid": vid, "status": "insufficient_peaks", "peaks": peaks}

    sig = file_sha256(vod)
    file_dur = _ffprobe_duration(vod)
    bounds = []
    for peak in peaks:
        start, dur, report = resolve_pubg_fight_bounds(vod, peak, file_duration=file_dur)
        start, dur = _tighten_owner_clip_bounds(start, dur, report)
        bounds.append(
            {
                "peak": peak,
                "start": start,
                "duration": dur,
                "end": start + dur,
                "shooting_start": report.get("shooting_start"),
                "fight_end": report.get("fight_end"),
                "report": report,
            }
        )

    result = {"vid": vid, "status": "dry_run" if dry_run else "pending", "peaks": peaks, "bounds": bounds}
    if dry_run:
        return result

    if not send:
        return result

    token = os.environ.get("TG_BOT_TOKEN", "")
    chat_id = _redo_chat_id()
    if not token or not chat_id:
        result["status"] = "no_telegram"
        return result

    log.info("redo send vod=%s chat_id=%s", vid, chat_id)
    clear_vod_sent(vod)
    rows = _peak_rows(vod, peaks, sig)
    for row, bound in zip(rows, bounds):
        dur = float(bound["duration"])
        start = float(bound["start"])
        row["start"] = start
        row["peak_start"] = bound["peak"]
        row["clip"] = {
            "start": start,
            "peak_start": bound["peak"],
            "input_duration": dur,
            "output_duration": dur,
            "fight_end": bound.get("fight_end"),
            "bounds_locked": True,
            "segment_report": bound.get("report") or {},
        }

    sent = _send_montage("pubg", token, chat_id, vod, rows, sig)
    result["status"] = "sent" if sent else "send_failed"
    result["sent"] = sent
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Redo owner-approved PUBG montages")
    parser.add_argument("--vods", nargs="*", default=list(DEFAULT_VODS))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-send", action="store_true")
    parser.add_argument("--merge-labels", action="store_true", default=True)
    args = parser.parse_args()

    _load_video_bot_env()
    _apply_redo_env()
    if args.merge_labels:
        merge_seed_labels("pubg")

    results = []
    for vid in args.vods:
        log.info("redo start vid=%s", vid)
        results.append(redo_vod(vid, dry_run=args.dry_run, send=not args.no_send))
        if not args.dry_run and not args.no_send:
            time.sleep(3.0)

    print(json.dumps(results, ensure_ascii=False, indent=2))
    failed = [r for r in results if r.get("status") not in ("sent", "dry_run")]
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
