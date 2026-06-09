#!/usr/bin/env python3
"""
Eval owner-label recall/precision on labeled VODs.

recall@good: fraction of good labels with a PASS candidate within ±tol sec
bad_precision: no PASS candidate within ±tol of bad labels
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from highlight_scorer import (
    WINDOW_SEC,
    _owner_labels_path,
    discover_highlight_candidates,
    normalize_profile,
    segment_overlaps_owner_label,
    vod_has_owner_labels,
)

INBOX = Path(os.environ.get("HIGHLIGHT_INBOX", "/root/data/mlbb/youtube_nightly/inbox"))
REPO = Path(os.environ.get("CONTENT_BOT_REPO", "/root/content_bot_ml"))

PROFILES = ("pubg", "standoff", "mobile_legends", "genshin", "wot")


def resolve_vod(video_id: str) -> Path | None:
    for p in (
        INBOX / f"yt_{video_id}.mp4",
        REPO / "data" / "samples" / f"yt_{video_id}.mp4",
    ):
        if p.exists():
            return p
    return None


def load_videos(profile: str) -> dict[str, list[dict]]:
    path = _owner_labels_path(profile)
    if not path or not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return data.get("videos", {})


def nearest_candidate(candidates: list[dict], time_sec: float, tol: float) -> dict | None:
    best: dict | None = None
    best_dist = tol + 1.0
    for cand in candidates:
        center = float(cand["start"]) + WINDOW_SEC * 0.5
        dist = abs(center - time_sec)
        if dist <= tol and dist < best_dist:
            best_dist = dist
            best = cand
    return best


def eval_vod(
    profile: str,
    video_id: str,
    rows: list[dict],
    *,
    good_tol: float,
    bad_tol: float,
) -> dict:
    vod = resolve_vod(video_id)
    if not vod:
        return {"video_id": video_id, "status": "vod_missing", "recall": 0.0, "bad_hits": 0}

    os.environ.setdefault("HIGHLIGHT_SOFT_ANCHOR", "1")
    os.environ.setdefault("HIGHLIGHT_USE_OWNER_ANCHORS", "0")
    candidates = discover_highlight_candidates(vod, profile, limit=24)

    good_rows = [r for r in rows if r.get("label") == "good" and "time_sec" in r]
    bad_rows = [r for r in rows if r.get("label") == "bad" and "time_sec" in r]

    good_hit = 0
    good_detail: list[str] = []
    for row in good_rows:
        t = float(row["time_sec"])
        hit = nearest_candidate(candidates, t, good_tol)
        if hit:
            good_hit += 1
            good_detail.append(f"OK@{t:.0f}")
        else:
            good_detail.append(f"MISS@{t:.0f}")

    bad_hits = 0
    bad_detail: list[str] = []
    for row in bad_rows:
        t = float(row["time_sec"])
        hit = nearest_candidate(candidates, t, bad_tol)
        if hit:
            bad_hits += 1
            bad_detail.append(f"HIT@{t:.0f}")
        else:
            bad_detail.append(f"OK@{t:.0f}")

    recall = good_hit / len(good_rows) if good_rows else 1.0
    bad_clean = 1.0 - (bad_hits / len(bad_rows)) if bad_rows else 1.0

    return {
        "video_id": video_id,
        "status": "ok",
        "vod": str(vod),
        "candidates": len(candidates),
        "good_total": len(good_rows),
        "good_hit": good_hit,
        "recall": recall,
        "bad_total": len(bad_rows),
        "bad_hits": bad_hits,
        "bad_clean": bad_clean,
        "good_detail": ",".join(good_detail[:12]),
        "bad_detail": ",".join(bad_detail[:12]),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", default="all", choices=["all", *PROFILES])
    parser.add_argument("--good-tol", type=float, default=90.0)
    parser.add_argument("--bad-tol", type=float, default=60.0)
    parser.add_argument("--csv", type=Path, default="")
    parser.add_argument("--min-recall", type=float, default=0.0, help="exit 1 if any VOD below")
    args = parser.parse_args()

    profiles = PROFILES if args.profile == "all" else (normalize_profile(args.profile),)
    rows_out: list[dict] = []

    print(f"{'profile':<16} {'video':<14} {'recall':>7} {'good':>8} {'bad_hit':>8} {'cand':>5}")
    print("-" * 72)

    worst_recall = 1.0
    for profile in profiles:
        videos = load_videos(profile)
        for vid, label_rows in videos.items():
            if not label_rows:
                continue
            vod_path = resolve_vod(vid)
            if vod_path and not vod_has_owner_labels(vod_path, profile):
                continue
            row = eval_vod(
                profile,
                vid,
                label_rows,
                good_tol=args.good_tol,
                bad_tol=args.bad_tol,
            )
            row["profile"] = profile
            rows_out.append(row)
            if row["status"] != "ok":
                print(f"{profile:<16} {vid:<14} {'—':>7} {'—':>8} {'—':>8} {'—':>5}  missing")
                continue
            recall = row["recall"]
            worst_recall = min(worst_recall, recall)
            print(
                f"{profile:<16} {vid:<14} {recall:>6.0%} "
                f"{row['good_hit']}/{row['good_total']:>5} "
                f"{row['bad_hits']}/{row['bad_total']:>5} "
                f"{row['candidates']:>5}"
            )

    if args.csv:
        args.csv.parent.mkdir(parents=True, exist_ok=True)
        with args.csv.open("w", newline="", encoding="utf-8") as handle:
            if rows_out:
                writer = csv.DictWriter(handle, fieldnames=list(rows_out[0].keys()))
                writer.writeheader()
                writer.writerows(rows_out)

    if args.min_recall > 0 and worst_recall < args.min_recall:
        print(f"FAIL min_recall={args.min_recall:.0%} worst={worst_recall:.0%}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
