#!/usr/bin/env python3
"""
Eval gate for highlight models — owner labels + silver reference (when available).

Pass criteria per game (deploy only if ALL pass):
  recall@good >= 0.70
  precision@bad >= 0.80  (no false pass on bad windows)
  montage_segments_found >= 3 on >=2 labeled VODs (discovery mode)
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from eval_owner_labels import PROFILES, eval_vod, load_videos, resolve_vod
from highlight_scorer import normalize_profile

REPO = Path(os.environ.get("CONTENT_BOT_REPO", "/root/content_bot_ml"))

PASS_RECALL = float(os.environ.get("EVAL_MIN_RECALL", "0.70"))
PASS_BAD_PREC = float(os.environ.get("EVAL_MIN_BAD_PREC", "0.80"))
PASS_MONTAGE_SEGS = int(os.environ.get("EVAL_MIN_MONTAGE_SEGS", "3"))
PASS_MIN_LABELED_VODS = int(os.environ.get("EVAL_MIN_LABELED_VODS", "2"))


def load_silver_features(profile: str) -> list[dict]:
    path = REPO / "data" / "viral_reference" / f"{profile}_features.csv"
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def eval_silver(profile: str) -> dict:
    rows = load_silver_features(profile)
    if not rows:
        return {"status": "no_silver", "top_pos_rate": 0.0, "bottom_neg_rate": 0.0}

    def fval(row: dict, key: str) -> float:
        try:
            return float(row.get(key) or 0)
        except (TypeError, ValueError):
            return 0.0

    scored = []
    for row in rows:
        views = fval(row, "view_count")
        hook = fval(row, "hook_score")
        combat = fval(row, "panns_gun_max") + fval(row, "clip_score") * 0.5
        scored.append((views * max(0.1, hook), combat, row))

    scored.sort(key=lambda x: x[0], reverse=True)
    n = max(1, len(scored) // 5)
    top = scored[:n]
    bottom = scored[-n:]

    top_pos = sum(1 for _, c, _ in top if c >= 0.15) / len(top)
    bottom_neg = sum(1 for _, c, _ in bottom if c < 0.15) / len(bottom)
    return {
        "status": "ok",
        "silver_total": len(rows),
        "top_pos_rate": round(top_pos, 4),
        "bottom_neg_rate": round(bottom_neg, 4),
        "top_n": len(top),
        "bottom_n": len(bottom),
    }


def eval_profile(profile: str, *, good_tol: float, bad_tol: float) -> dict:
    profile = normalize_profile(profile)
    videos = load_videos(profile)
    vod_results: list[dict] = []
    labeled_ok = 0

    for vid, rows in videos.items():
        if not rows:
            continue
        if resolve_vod(vid) is None:
            continue
        row = eval_vod(profile, vid, rows, good_tol=good_tol, bad_tol=bad_tol)
        row["profile"] = profile
        vod_results.append(row)
        if row.get("status") == "ok":
            labeled_ok += 1

    recalls = [r["recall"] for r in vod_results if r.get("status") == "ok" and r.get("good_total", 0) > 0]
    bad_precs = [r["bad_clean"] for r in vod_results if r.get("status") == "ok" and r.get("bad_total", 0) > 0]
    montage_ok = sum(
        1 for r in vod_results if r.get("status") == "ok" and r.get("candidates", 0) >= PASS_MONTAGE_SEGS
    )

    worst_recall = min(recalls) if recalls else 0.0
    worst_bad_prec = min(bad_precs) if bad_precs else 1.0

    silver = eval_silver(profile)
    silver_pass = True
    if silver["status"] == "ok":
        silver_pass = silver["top_pos_rate"] >= 0.60 and silver["bottom_neg_rate"] >= 0.80

    owner_pass = (
        worst_recall >= PASS_RECALL
        and worst_bad_prec >= PASS_BAD_PREC
        and montage_ok >= PASS_MIN_LABELED_VODS
    )
    all_pass = owner_pass and (silver["status"] != "ok" or silver_pass)

    misses: list[str] = []
    for r in vod_results:
        if r.get("status") != "ok":
            continue
        if r.get("recall", 1) < PASS_RECALL:
            misses.append(f"{r['video_id']}: {r.get('good_detail', '')}")
        if r.get("bad_clean", 1) < PASS_BAD_PREC:
            misses.append(f"{r['video_id']}: {r.get('bad_detail', '')}")

    return {
        "profile": profile,
        "labeled_vods": labeled_ok,
        "worst_recall": round(worst_recall, 4),
        "worst_bad_precision": round(worst_bad_prec, 4),
        "montage_vods_ge3": montage_ok,
        "silver": silver,
        "owner_pass": owner_pass,
        "silver_pass": silver_pass,
        "all_pass": all_pass,
        "miss_windows": misses[:20],
        "vods": vod_results,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", default="all", choices=["all", *PROFILES])
    parser.add_argument("--good-tol", type=float, default=90.0)
    parser.add_argument("--bad-tol", type=float, default=60.0)
    parser.add_argument("--report", type=Path, default=None)
    parser.add_argument("--require-pass", action="store_true")
    args = parser.parse_args()

    profiles = PROFILES if args.profile == "all" else (normalize_profile(args.profile),)
    report_path = args.report or (REPO / "data" / "training" / "eval_latest.json")

    results: list[dict] = []
    all_pass = True

    print(
        f"{'profile':<16} {'recall':>7} {'bad_prec':>9} {'montage':>8} {'silver':>8} {'PASS':>6}"
    )
    print("-" * 62)

    for profile in profiles:
        r = eval_profile(profile, good_tol=args.good_tol, bad_tol=args.bad_tol)
        results.append(r)
        if not r["all_pass"]:
            all_pass = False
        silver_tag = r["silver"]["status"]
        if silver_tag == "ok":
            silver_tag = f"{r['silver']['top_pos_rate']:.0%}/{r['silver']['bottom_neg_rate']:.0%}"
        print(
            f"{profile:<16} {r['worst_recall']:>6.0%} {r['worst_bad_precision']:>8.0%} "
            f"{r['montage_vods_ge3']:>8} {silver_tag:>8} "
            f"{'YES' if r['all_pass'] else 'NO':>6}"
        )
        for miss in r.get("miss_windows", [])[:4]:
            print(f"  miss: {miss}")

    payload = {
        "date": date.today().isoformat(),
        "criteria": {
            "recall_min": PASS_RECALL,
            "bad_precision_min": PASS_BAD_PREC,
            "montage_segments_min": PASS_MONTAGE_SEGS,
            "labeled_vods_min": PASS_MIN_LABELED_VODS,
        },
        "all_pass": all_pass,
        "profiles": results,
    }

    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"report={report_path} all_pass={all_pass}")

    if args.require_pass and not all_pass:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
