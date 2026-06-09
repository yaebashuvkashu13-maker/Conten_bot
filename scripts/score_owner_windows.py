#!/usr/bin/env python3
"""Score owner-labeled windows — fast path + baseline eval report."""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from highlight_scorer import (
    WINDOW_SEC,
    _owner_anchor_starts,
    _owner_labels_path,
    normalize_profile,
    score_candidate_window,
)

REPO = Path(os.environ.get("CONTENT_BOT_REPO", "/root/content_bot_ml"))
INBOX = Path(os.environ.get("HIGHLIGHT_INBOX", "/root/data/mlbb/youtube_nightly/inbox"))
PROFILES = ("pubg", "standoff", "mobile_legends", "genshin", "wot")


def resolve_vod(video_id: str) -> Path | None:
    for candidate in (
        INBOX / f"yt_{video_id}.mp4",
        REPO / "data" / "samples" / f"yt_{video_id}.mp4",
    ):
        if candidate.exists():
            return candidate
    return None


def score_window_row(
    profile: str,
    video_id: str,
    label: str,
    time_sec: float,
    vod: Path,
) -> dict:
    start = max(0.0, float(time_sec) - WINDOW_SEC * 0.5)
    m = score_candidate_window(vod, start, WINDOW_SEC, profile)
    pass_gate = bool(m.rule_pass and m.visual_pass)
    return {
        "profile": profile,
        "video_id": video_id,
        "label": label,
        "time_sec": int(time_sec),
        "start": round(start, 1),
        "pass": pass_gate,
        "rule_pass": bool(m.rule_pass),
        "visual_pass": bool(m.visual_pass),
        "pass_reason": m.pass_reason or "",
        "clip_score": round(float(m.clip_score), 4),
        "panns_gunshot": round(float(m.panns_gunshot), 4),
        "panns_machine_gun": round(float(m.panns_machine_gun), 4),
        "panns_gun_max": round(float(m.panns_gun_max), 4),
        "center_motion": round(float(m.center_motion), 4),
        "hook_score": round(float(m.hook_score), 4),
        "classifier_prob": round(float(m.classifier_prob), 4),
    }


def baseline_eval(
    profiles: tuple[str, ...],
    *,
    csv_path: Path | None = None,
) -> list[dict]:
    rows: list[dict] = []
    summaries: list[dict] = []

    for profile in profiles:
        path = _owner_labels_path(profile)
        if not path or not path.exists():
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        videos = data.get("videos", {})
        for video_id, label_rows in videos.items():
            vod = resolve_vod(str(video_id))
            if not vod:
                summaries.append(
                    {
                        "profile": profile,
                        "video_id": video_id,
                        "status": "vod_missing",
                        "good_total": 0,
                        "good_pass": 0,
                        "bad_total": 0,
                        "bad_false_pass": 0,
                    }
                )
                continue

            good_pass = good_total = bad_false = bad_total = 0
            clip_good: list[float] = []
            clip_bad: list[float] = []
            pann_good: list[float] = []
            pann_bad: list[float] = []

            for row in label_rows:
                label = row.get("label")
                if label not in ("good", "bad") or "time_sec" not in row:
                    continue
                t = float(row["time_sec"])
                scored = score_window_row(profile, str(video_id), label, t, vod)
                rows.append(scored)
                if label == "good":
                    good_total += 1
                    if scored["pass"]:
                        good_pass += 1
                    clip_good.append(scored["clip_score"])
                    pann_good.append(scored["panns_gun_max"])
                else:
                    bad_total += 1
                    if scored["pass"]:
                        bad_false += 1
                    clip_bad.append(scored["clip_score"])
                    pann_bad.append(scored["panns_gun_max"])

            recall = good_pass / good_total if good_total else 1.0
            bad_precision = 1.0 - (bad_false / bad_total) if bad_total else 1.0
            summaries.append(
                {
                    "profile": profile,
                    "video_id": video_id,
                    "status": "ok",
                    "good_total": good_total,
                    "good_pass": good_pass,
                    "recall_at_good": round(recall, 4),
                    "bad_total": bad_total,
                    "bad_false_pass": bad_false,
                    "bad_precision": round(bad_precision, 4),
                    "avg_clip_good": round(sum(clip_good) / len(clip_good), 4) if clip_good else 0.0,
                    "avg_clip_bad": round(sum(clip_bad) / len(clip_bad), 4) if clip_bad else 0.0,
                    "avg_pann_good": round(sum(pann_good) / len(pann_good), 4) if pann_good else 0.0,
                    "avg_pann_bad": round(sum(pann_bad) / len(pann_bad), 4) if pann_bad else 0.0,
                }
            )

    print(f"{'profile':<16} {'video':<14} {'recall':>7} {'good':>8} {'bad_fp':>8} {'clip_g':>7} {'clip_b':>7}")
    print("-" * 78)
    for s in summaries:
        if s["status"] != "ok":
            print(f"{s['profile']:<16} {s['video_id']:<14} {'—':>7} {'—':>8} {'—':>8} {'—':>7} {'—':>7}  missing")
            continue
        print(
            f"{s['profile']:<16} {s['video_id']:<14} {s['recall_at_good']:>6.0%} "
            f"{s['good_pass']}/{s['good_total']:>5} "
            f"{s['bad_false_pass']}/{s['bad_total']:>5} "
            f"{s['avg_clip_good']:>7.3f} {s['avg_clip_bad']:>7.3f}"
        )

    if csv_path:
        csv_path.parent.mkdir(parents=True, exist_ok=True)
        all_rows = rows + [
            {**s, "row_type": "summary"}
            for s in summaries
        ]
        fieldnames = sorted({k for r in all_rows for k in r})
        with csv_path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            for r in rows:
                writer.writerow({**r, "row_type": "window"})
            for s in summaries:
                writer.writerow({**s, "row_type": "summary"})

    return summaries


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", default="")
    parser.add_argument("--vod", default="")
    parser.add_argument(
        "--baseline",
        action="store_true",
        help="Score all owner good/bad windows and write baseline CSV",
    )
    parser.add_argument("--csv", type=Path, default=None)
    args = parser.parse_args()

    if args.baseline:
        profiles = PROFILES if not args.profile else (normalize_profile(args.profile),)
        csv_out = args.csv
        if csv_out is None:
            csv_out = REPO / "data" / "training" / f"eval_baseline_{date.today().isoformat()}.csv"
        baseline_eval(profiles, csv_path=csv_out)
        print(f"baseline_csv={csv_out}")
        return 0

    if not args.profile or not args.vod:
        print("REFUSED: provide --profile and --vod, or use --baseline")
        return 1

    vod = Path(args.vod)
    if not vod.exists():
        vod = INBOX / args.vod if not args.vod.startswith("yt_") else INBOX / args.vod
    if not vod.exists():
        vod = INBOX / f"yt_{args.vod}.mp4"
    if not vod.exists():
        print(f"REFUSED vod_missing {vod}")
        return 1

    profile = normalize_profile(args.profile)
    anchors = _owner_anchor_starts(vod, profile)
    if not anchors:
        print(f"REFUSED no_owner_anchors profile={profile}")
        return 1

    passed = 0
    for t in anchors:
        m = score_candidate_window(vod, max(0.0, t - 2.0), WINDOW_SEC, profile)
        ok = m.rule_pass and m.visual_pass
        if ok:
            passed += 1
        print(
            f"start={int(t)} pass={ok} rule={m.rule_pass} visual={m.visual_pass} "
            f"reason={m.pass_reason} clip={m.clip_score:.3f} "
            f"mini={m.minimap_delta:.4f} skill={m.skill_delta:.4f}"
        )
    print(f"SUMMARY passed={passed}/{len(anchors)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
