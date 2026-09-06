#!/usr/bin/env python3
"""Regression benchmark for PUBG generator/ranker/presend on owner timestamps."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from pubg_moment_ranker import rank_peaks_with_model


def _labels(path: Path) -> dict[str, list[dict]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    videos = data.get("videos")
    return videos if isinstance(videos, dict) else {}


def resolve_vod(video_id: str, root: Path) -> Path | None:
    candidates = [
        root / f"yt_{video_id}.mp4",
        Path("/root/data/pubg/youtube_nightly/inbox") / f"yt_{video_id}.mp4",
        Path("/root/data/pubg/youtube_nightly/parked") / f"yt_{video_id}.mp4",
        Path("/root/data/pubg/youtube_nightly/park_timeout") / f"yt_{video_id}.mp4",
        Path("/root/content_bot_ml/data/samples") / f"yt_{video_id}.mp4",
    ]
    return next((path for path in candidates if path.is_file()), None)


def restore_missing(video_ids: list[str], root: Path) -> dict[str, str]:
    from youtube_download import download_one, load_env

    root.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, **load_env(Path("/root/.video_bot.env"))}
    restored: dict[str, str] = {}
    for video_id in video_ids:
        if resolve_vod(video_id, root):
            continue
        try:
            path = download_one(
                f"https://www.youtube.com/watch?v={video_id}",
                root,
                env,
            )
            target = root / f"yt_{video_id}.mp4"
            if path != target:
                path.replace(target)
            restored[video_id] = str(target)
        except Exception as exc:
            restored[video_id] = f"ERROR: {exc}"
    return restored


def _nearest(peaks: list[float], target: float, tolerance: float) -> float | None:
    nearby = [peak for peak in peaks if abs(float(peak) - target) <= tolerance]
    return min(nearby, key=lambda peak: abs(float(peak) - target)) if nearby else None


def apply_online_overrides(
    labels: dict[str, list[dict]],
    online_path: Path | None,
) -> tuple[dict[str, list[dict]], list[dict]]:
    if online_path is None or not online_path.is_file():
        return labels, []
    online = _labels(online_path)
    adjusted: dict[str, list[dict]] = {}
    conflicts: list[dict] = []
    for video_id, rows in labels.items():
        latest: dict[int, str] = {}
        for row in online.get(video_id, []):
            if "time_sec" in row and row.get("label") in ("good", "bad"):
                latest[round(float(row["time_sec"]))] = str(row["label"])
        adjusted[video_id] = []
        for source in rows:
            row = dict(source)
            current = latest.get(round(float(row.get("time_sec", -999))))
            if current and current != row.get("label"):
                conflicts.append(
                    {
                        "video_id": video_id,
                        "time_sec": row.get("time_sec"),
                        "immutable_label": row.get("label"),
                        "online_label": current,
                    }
                )
                row["immutable_label"] = row.get("label")
                row["label"] = current
                row["online_override"] = True
            adjusted[video_id].append(row)
    return adjusted, conflicts


def rescore_vod_result(
    result: dict,
    rows: list[dict],
    *,
    tolerance: float,
) -> dict:
    updated = dict(result)
    peaks = [float(value) for value in result.get("peaks") or []]
    ranked = [float(value) for value in result.get("ranked_peaks") or []]
    accepted = [
        float(row["peak"])
        for row in result.get("quality") or []
        if row.get("accepted")
    ]
    good = [float(row["time_sec"]) for row in rows if row.get("label") == "good"]
    bad = [float(row["time_sec"]) for row in rows if row.get("label") == "bad"]
    updated.update(
        {
            "good_total": len(good),
            "good_generator_hits": sum(
                _nearest(peaks, target, tolerance) is not None for target in good
            ),
            "good_top10_hits": sum(
                _nearest(ranked[:10], target, tolerance) is not None for target in good
            ),
            "good_accepted_hits": sum(
                _nearest(accepted, target, tolerance) is not None for target in good
            ),
            "bad_total": len(bad),
            "bad_accepted_hits": sum(
                _nearest(accepted, target, tolerance) is not None for target in bad
            ),
        }
    )
    return updated


def benchmark_vod(
    video_id: str,
    rows: list[dict],
    vod: Path,
    *,
    tolerance: float,
) -> dict:
    from shooter_vod_fast_scan import discover_montage_gun_peaks
    from smart_video_editor import ffprobe_duration
    from pubg_fight_segment import resolve_pubg_fight_bounds
    from pubg_quality_score import score_pubg_window
    from vod_quality import dense_probe_passes

    os.environ["HIGHLIGHT_USE_OWNER_ANCHORS"] = "0"
    os.environ["PUBG_OWNER_ANCHORS"] = "0"
    os.environ["PUBG_OWNER_BAD_HARD_REJECT"] = "0"
    peaks: list[float] = []
    generator_reasons: list[str] = []
    passes = (
        1
        if os.environ.get("SHOOTER_VOD_AUDIO_GENERATOR", "1") == "1"
        else dense_probe_passes()
    )
    for probe_pass in range(passes):
        pass_peaks, pass_reason = discover_montage_gun_peaks(
            vod,
            "pubg",
            min_clips=2,
            gap_sec=float(os.environ.get("SHOOTER_VOD_MONTAGE_GAP_SEC", "55")),
            probe_pass=probe_pass,
        )
        generator_reasons.append(pass_reason)
        for peak in pass_peaks:
            if not any(abs(float(peak) - old) <= 4.0 for old in peaks):
                peaks.append(float(peak))
    generator_reason = " | ".join(generator_reasons)
    ranked, ranker_reason = rank_peaks_with_model(vod, peaks, part_sec=14.0)
    accepted: list[float] = []
    quality: list[dict] = []
    duration = float(ffprobe_duration(vod))
    good = [float(row["time_sec"]) for row in rows if row.get("label") == "good"]
    bad = [float(row["time_sec"]) for row in rows if row.get("label") == "bad"]
    targets = good + bad
    quality_peaks = [
        peak
        for index, peak in enumerate(ranked)
        if index < 10 or any(abs(float(peak) - target) <= tolerance for target in targets)
    ]
    for peak in quality_peaks:
        start, clip_duration, segment_report = resolve_pubg_fight_bounds(
            vod,
            float(peak),
            file_duration=duration,
        )
        ok, reason, report = score_pubg_window(vod, start, clip_duration)
        quality.append(
            {
                "peak": round(float(peak), 2),
                "start": start,
                "duration": clip_duration,
                "accepted": ok,
                "reason": reason,
                "score": report.get("quality_score"),
                "segmenter": segment_report,
            }
        )
        if ok:
            accepted.append(float(peak))

    good_generator_hits = sum(_nearest(peaks, target, tolerance) is not None for target in good)
    good_top10_hits = sum(_nearest(ranked[:10], target, tolerance) is not None for target in good)
    good_accepted_hits = sum(_nearest(accepted, target, tolerance) is not None for target in good)
    bad_accepted_hits = sum(_nearest(accepted, target, tolerance) is not None for target in bad)
    return {
        "video_id": video_id,
        "vod": str(vod),
        "generator_reason": generator_reason,
        "ranker_reason": ranker_reason,
        "candidate_count": len(peaks),
        "accepted_count": len(accepted),
        "good_total": len(good),
        "good_generator_hits": good_generator_hits,
        "good_top10_hits": good_top10_hits,
        "good_accepted_hits": good_accepted_hits,
        "bad_total": len(bad),
        "bad_accepted_hits": bad_accepted_hits,
        "peaks": [round(float(peak), 2) for peak in peaks],
        "ranked_peaks": [round(float(peak), 2) for peak in ranked],
        "quality": quality,
    }


def aggregate(rows: list[dict]) -> dict:
    good = sum(row["good_total"] for row in rows)
    bad = sum(row["bad_total"] for row in rows)
    return {
        "videos": len(rows),
        "good_total": good,
        "bad_total": bad,
        "generator_recall": (
            sum(row["good_generator_hits"] for row in rows) / good if good else 1.0
        ),
        "ranker_recall_at_10": (
            sum(row["good_top10_hits"] for row in rows) / good if good else 1.0
        ),
        "accepted_recall": (
            sum(row["good_accepted_hits"] for row in rows) / good if good else 1.0
        ),
        "bad_accept_rate": (
            sum(row["bad_accepted_hits"] for row in rows) / bad if bad else 0.0
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--vod-root", type=Path, default=Path("/root/data/pubg/regression_vods"))
    parser.add_argument(
        "--labels",
        type=Path,
        default=Path(
            os.environ.get(
                "PUBG_REGRESSION_LABELS",
                str(Path(__file__).resolve().parent.parent / "data" / "pubg_regression_labels.json"),
            )
        ),
    )
    parser.add_argument("--restore-missing", action="store_true")
    parser.add_argument("--tolerance-sec", type=float, default=45.0)
    parser.add_argument("--output", type=Path, default=Path("/root/data/pubg/regression_report.json"))
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--online-labels", type=Path)
    parser.add_argument("--rescore", type=Path)
    parser.add_argument("--max-recall-drop", type=float, default=0.05)
    args = parser.parse_args()

    immutable_labels = _labels(args.labels)
    labels, label_conflicts = apply_online_overrides(
        immutable_labels,
        args.online_labels,
    )
    if args.restore_missing:
        restore_report = restore_missing(list(labels), args.vod_root)
    else:
        restore_report = {}
    results: list[dict] = []
    missing: list[str] = []
    started = time.monotonic()
    if args.rescore and args.rescore.is_file():
        previous = json.loads(args.rescore.read_text(encoding="utf-8"))
        for row in previous.get("vods") or []:
            video_id = str(row.get("video_id") or "")
            results.append(
                rescore_vod_result(
                    row,
                    labels.get(video_id, []),
                    tolerance=max(1.0, args.tolerance_sec),
                )
            )
    else:
        for video_id, label_rows in labels.items():
            vod = resolve_vod(video_id, args.vod_root)
            if vod is None:
                missing.append(video_id)
                continue
            results.append(
                benchmark_vod(
                    video_id,
                    label_rows,
                    vod,
                    tolerance=max(1.0, args.tolerance_sec),
                )
            )
    summary = aggregate(results)
    report = {
        "benchmark": "pubg_owner_regression_v1",
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "elapsed_sec": round(time.monotonic() - started, 2),
        "labels_file": str(args.labels),
        "expected_labels": sum(len(rows) for rows in immutable_labels.values()),
        "online_label_conflicts": label_conflicts,
        "missing_vods": missing,
        "restore": restore_report,
        "summary": summary,
        "vods": results,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))

    if missing:
        return 2
    if args.baseline and args.baseline.is_file():
        baseline = json.loads(args.baseline.read_text(encoding="utf-8")).get("summary", {})
        for metric in ("generator_recall", "ranker_recall_at_10", "accepted_recall"):
            if summary[metric] + args.max_recall_drop < float(baseline.get(metric, 0.0)):
                print(f"REGRESSION {metric}: {summary[metric]:.3f} < {baseline[metric]:.3f}")
                return 1
        if summary["bad_accept_rate"] > float(baseline.get("bad_accept_rate", 1.0)) + 0.05:
            print("REGRESSION bad_accept_rate")
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
