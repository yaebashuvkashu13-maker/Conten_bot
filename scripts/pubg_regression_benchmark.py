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


def benchmark_vod(
    video_id: str,
    rows: list[dict],
    vod: Path,
    *,
    tolerance: float,
) -> dict:
    from shooter_vod_fast_scan import discover_montage_gun_peaks
    from pubg_quality_score import score_pubg_window

    os.environ["HIGHLIGHT_USE_OWNER_ANCHORS"] = "0"
    os.environ["PUBG_OWNER_ANCHORS"] = "0"
    os.environ["PUBG_OWNER_BAD_HARD_REJECT"] = "0"
    peaks, generator_reason = discover_montage_gun_peaks(
        vod,
        "pubg",
        min_clips=2,
        gap_sec=float(os.environ.get("SHOOTER_VOD_MONTAGE_GAP_SEC", "55")),
        probe_pass=0,
    )
    ranked, ranker_reason = rank_peaks_with_model(vod, peaks, part_sec=14.0)
    accepted: list[float] = []
    quality: list[dict] = []
    for peak in ranked:
        start = max(0.0, float(peak) - 7.0)
        ok, reason, report = score_pubg_window(vod, start, 14.0)
        quality.append(
            {
                "peak": round(float(peak), 2),
                "accepted": ok,
                "reason": reason,
                "score": report.get("quality_score"),
            }
        )
        if ok:
            accepted.append(float(peak))

    good = [float(row["time_sec"]) for row in rows if row.get("label") == "good"]
    bad = [float(row["time_sec"]) for row in rows if row.get("label") == "bad"]
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
    parser.add_argument("--tolerance-sec", type=float, default=30.0)
    parser.add_argument("--output", type=Path, default=Path("/root/data/pubg/regression_report.json"))
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--max-recall-drop", type=float, default=0.05)
    args = parser.parse_args()

    labels = _labels(args.labels)
    if args.restore_missing:
        restore_report = restore_missing(list(labels), args.vod_root)
    else:
        restore_report = {}
    results: list[dict] = []
    missing: list[str] = []
    started = time.monotonic()
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
        "expected_labels": sum(len(rows) for rows in labels.values()),
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
