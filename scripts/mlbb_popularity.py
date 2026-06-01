#!/usr/bin/env python3
"""
Popularity index for MLBB TikTok clips — learn from CSV + tag each download.

Files:
  /root/data/mlbb/popularity_lookup.json   video_id -> metrics
  /root/data/mlbb/downloaded_popularity.jsonl  one row per saved mp4
"""

from __future__ import annotations

import csv
import json
import re
import time
from pathlib import Path

RANKED_CSV = Path("/root/data/mlbb/current_mlbb_ranked_videos.csv")
GAMEPLAY_CSV = Path("/root/data/mlbb/gameplay_filter_latest.csv")
LOOKUP_PATH = Path("/root/data/mlbb/popularity_lookup.json")
REGISTRY_PATH = Path("/root/data/mlbb/downloaded_popularity.jsonl")
SUMMARY_PATH = Path("/root/data/mlbb/popularity_summary.json")


def _int(val: object, default: int = 0) -> int:
    try:
        return int(float(str(val or default)))
    except (TypeError, ValueError):
        return default


def _float(val: object, default: float = 0.0) -> float:
    try:
        return float(str(val or default))
    except (TypeError, ValueError):
        return default


def extract_video_id(text: str) -> str | None:
    match = re.search(r"(\d{10,22})", text)
    return match.group(1) if match else None


def build_lookup_from_csvs() -> dict[str, dict]:
    lookup: dict[str, dict] = {}

    def ingest(path: Path, source: str) -> None:
        if not path.exists():
            return
        with path.open(encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                vid = str(row.get("video_id") or "").strip()
                if not vid:
                    vid = extract_video_id(str(row.get("webpage_url") or "")) or ""
                if not vid:
                    continue
                views = _int(row.get("view_count") or row.get("views"))
                likes = _int(row.get("like_count") or row.get("likes"))
                score = _float(row.get("score") or row.get("gameplay_score"))
                prev = lookup.get(vid, {})
                lookup[vid] = {
                    "video_id": vid,
                    "views": max(views, _int(prev.get("views"))),
                    "likes": max(likes, _int(prev.get("likes"))),
                    "csv_score": max(score, _float(prev.get("csv_score"))),
                    "source": source if source not in str(prev.get("source", "")) else f"{prev.get('source')},{source}",
                    "description": (row.get("description") or prev.get("description") or "")[:200],
                    "url": row.get("webpage_url") or prev.get("url") or "",
                }

    ingest(RANKED_CSV, "ranked")
    ingest(GAMEPLAY_CSV, "gameplay_filter")
    return lookup


def save_lookup(lookup: dict[str, dict]) -> None:
    LOOKUP_PATH.parent.mkdir(parents=True, exist_ok=True)
    ranked = sorted(
        lookup.values(),
        key=lambda item: (_int(item.get("views")), _int(item.get("likes")), _float(item.get("csv_score"))),
        reverse=True,
    )
    for idx, item in enumerate(ranked, start=1):
        item["rank"] = idx
        views = max(_int(item.get("views")), 1)
        likes = _int(item.get("likes"))
        item["engagement_rate"] = round(likes / views, 6)
        item["popularity_score"] = round(
            _float(item.get("csv_score")) * 0.45
            + min(1.0, views / 500_000) * 0.35
            + min(1.0, likes / max(views // 50, 1)) * 0.20,
            4,
        )
    LOOKUP_PATH.write_text(json.dumps({"updated_at": time.strftime("%Y-%m-%d %H:%M:%S"), "items": lookup}, ensure_ascii=False, indent=2))
    top = ranked[:50]
    SUMMARY_PATH.write_text(
        json.dumps(
            {
                "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "total_indexed": len(lookup),
                "top50": top,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


def load_lookup() -> dict[str, dict]:
    if LOOKUP_PATH.exists():
        try:
            payload = json.loads(LOOKUP_PATH.read_text(encoding="utf-8"))
            return payload.get("items", payload) if isinstance(payload, dict) else {}
        except json.JSONDecodeError:
            pass
    return build_lookup_from_csvs()


def record_download(path: Path, description: str = "") -> dict | None:
    lookup = load_lookup()
    if not lookup:
        lookup = build_lookup_from_csvs()
        save_lookup(lookup)

    vid = extract_video_id(f"{path.name} {description} {path}")
    if not vid:
        return None
    meta = lookup.get(vid, {"video_id": vid, "views": 0, "likes": 0, "csv_score": 0.0, "popularity_score": 0.0})
    row = {
        "video_id": vid,
        "path": str(path),
        "views": meta.get("views", 0),
        "likes": meta.get("likes", 0),
        "popularity_score": meta.get("popularity_score", 0.0),
        "rank": meta.get("rank"),
        "recorded_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
    with REGISTRY_PATH.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    return row


def popularity_boost(video_id: str) -> float:
    """0..~0.35 extra for smart edit candidate scoring."""
    lookup = load_lookup()
    meta = lookup.get(video_id)
    if not meta:
        return 0.0
    return min(0.35, _float(meta.get("popularity_score")) * 0.35)


def sync_all_downloads(inbox: Path | None = None) -> dict:
    inbox = inbox or Path("/root/datasets/tiktok/mlbb")
    lookup = build_lookup_from_csvs()
    save_lookup(lookup)

    known_paths: set[str] = set()
    if REGISTRY_PATH.exists():
        for line in REGISTRY_PATH.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                known_paths.add(json.loads(line).get("path", ""))
            except json.JSONDecodeError:
                continue

    added = 0
    for video in sorted(inbox.rglob("*.mp4")):
        if "non_gameplay" in video.parts:
            continue
        if str(video) in known_paths:
            continue
        if record_download(video):
            added += 1
    return {"lookup_size": len(lookup), "registry_added": added, "top_score": lookup[max(lookup, key=lambda k: _float(lookup[k].get('popularity_score')))]["popularity_score"] if lookup else 0}


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--sync-downloads", action="store_true")
    parser.add_argument("--rebuild", action="store_true")
    args = parser.parse_args()

    if args.rebuild or not LOOKUP_PATH.exists():
        lookup = build_lookup_from_csvs()
        save_lookup(lookup)
        print(json.dumps({"rebuilt": len(lookup)}, ensure_ascii=False))
    if args.sync_downloads:
        print(json.dumps(sync_all_downloads(), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
