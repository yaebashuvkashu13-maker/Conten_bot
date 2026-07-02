#!/usr/bin/env python3
"""
Diagnose PUBG VOD moment search vs owner labels.

Usage:
  python3 pubg_vod_diagnose.py n97cHIR9Qow pJ-X6NdSU9k
  python3 pubg_vod_diagnose.py --download n97cHIR9Qow
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from highlight_scorer import (
    WINDOW_SEC,
    _labels_for_vod,
    discover_highlight_candidates,
    normalize_profile,
    score_panns_audio,
    stage1_candidates,
    stage1_panns_prefilter,
)
from shooter_vod_fast_scan import vod_fast_combat_check

REPO = Path(os.environ.get("CONTENT_BOT_REPO", Path(__file__).resolve().parent.parent))
PUBG_INBOX = Path(os.environ.get("PUBG_VOD_INBOX", "/root/data/pubg/youtube_nightly/inbox"))
LABELS = REPO / "data" / "pubg_owner_labels.json"


def resolve_vod(video_id: str) -> Path | None:
    for base in (PUBG_INBOX, REPO / "data" / "samples"):
        p = base / f"yt_{video_id}.mp4"
        if p.exists() and p.stat().st_size > 100_000:
            return p
    return None


def download_vod(video_id: str) -> Path | None:
    PUBG_INBOX.mkdir(parents=True, exist_ok=True)
    out = PUBG_INBOX / f"yt_{video_id}.mp4"
    if out.exists() and out.stat().st_size > 100_000:
        return out
    url = f"https://www.youtube.com/watch?v={video_id}"
    cmd = [
        "yt-dlp",
        "-f",
        "best[height<=720]/best",
        "--no-playlist",
        "-o",
        str(out),
        url,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    if proc.returncode != 0 or not out.exists():
        print(f"download failed {video_id}: {(proc.stderr or proc.stdout)[:300]}")
        return None
    return out


def nearest(candidates: list[dict], time_sec: float, tol: float) -> dict | None:
    best: dict | None = None
    best_d = tol + 1.0
    for cand in candidates:
        center = float(cand["start"]) + WINDOW_SEC * 0.5
        d = abs(center - time_sec)
        if d <= tol and d < best_d:
            best_d = d
            best = cand
    return best


def diagnose_one(video_id: str, *, download: bool, tol: float) -> dict:
    vod = resolve_vod(video_id)
    if vod is None and download:
        vod = download_vod(video_id)
    if vod is None:
        return {"video_id": video_id, "status": "missing"}

    profile = "pubg"
    os.environ.setdefault("HIGHLIGHT_HEATMAP", "0")
    os.environ.setdefault("HIGHLIGHT_USE_OWNER_ANCHORS", "1")
    os.environ.setdefault("HIGHLIGHT_SOFT_ANCHOR", "1")
    os.environ["HIGHLIGHT_BUILD_POOL"] = "1"
    os.environ.setdefault("SHOOTER_VOD_SCORE_MAX", "8")

    labels = _labels_for_vod(vod, profile)
    if not labels and LABELS.exists():
        data = json.loads(LABELS.read_text(encoding="utf-8"))
        labels = list(data.get("videos", {}).get(video_id, []))

    fast_ok, fast_reason, fast_peaks = vod_fast_combat_check(vod, profile)
    stage1 = stage1_candidates(vod, profile)
    prefilter = stage1_panns_prefilter(vod, stage1, profile)
    candidates = discover_highlight_candidates(vod, profile, limit=16)

    rows: list[dict] = []
    for lab in labels:
        t = float(lab["time_sec"])
        panns = score_panns_audio(vod, max(0.0, t - WINDOW_SEC * 0.5), WINDOW_SEC)
        cand = nearest(candidates, t, tol)
        rows.append(
            {
                "tc": lab.get("tc", ""),
                "time_sec": t,
                "label": lab.get("label"),
                "panns_gun_max": round(float(panns.get("panns_gun_max", 0)), 4),
                "stage1_near": any(abs(s - t) <= 90 for s in stage1),
                "prefilter_near": any(abs(s - t) <= 90 for s in prefilter),
                "candidate": cand is not None,
                "cand_start": round(float(cand["start"]), 1) if cand else None,
                "cand_reason": (cand.get("highlight_metrics") or {}).get("pass_reason") if cand else None,
            }
        )

    good = [r for r in rows if r["label"] == "good"]
    recall = sum(1 for r in good if r["candidate"]) / max(1, len(good))

    return {
        "video_id": video_id,
        "path": str(vod),
        "fast_ok": fast_ok,
        "fast_reason": fast_reason,
        "fast_peaks": fast_peaks[:8],
        "stage1_count": len(stage1),
        "prefilter_count": len(prefilter),
        "candidate_count": len(candidates),
        "recall_good": round(recall, 3),
        "labels": rows,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="PUBG VOD moment diagnose vs owner labels")
    ap.add_argument("video_ids", nargs="+", help="YouTube video IDs")
    ap.add_argument("--download", action="store_true", help="yt-dlp missing VODs")
    ap.add_argument("--tol", type=float, default=45.0, help="match tolerance sec")
    ap.add_argument("--json", action="store_true", help="JSON output")
    args = ap.parse_args()

    reports = [diagnose_one(vid, download=args.download, tol=args.tol) for vid in args.video_ids]
    if args.json:
        print(json.dumps(reports, indent=2, ensure_ascii=False))
    else:
        for rep in reports:
            print(f"\n=== {rep.get('video_id')} ===")
            if rep.get("status") == "missing":
                print("  VOD missing (use --download)")
                continue
            print(f"  fast: {rep.get('fast_ok')} {rep.get('fast_reason')}")
            print(
                f"  stage1={rep.get('stage1_count')} prefilter={rep.get('prefilter_count')} "
                f"candidates={rep.get('candidate_count')} recall_good={rep.get('recall_good')}"
            )
            for row in rep.get("labels", []):
                hit = "HIT" if row.get("candidate") else "MISS"
                print(
                    f"  [{hit}] {row.get('tc')} {row.get('label')} "
                    f"panns={row.get('panns_gun_max')} s1={row.get('stage1_near')} "
                    f"pf={row.get('prefilter_near')}"
                )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
