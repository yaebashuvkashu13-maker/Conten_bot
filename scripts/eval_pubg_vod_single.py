#!/usr/bin/env python3
"""Eval one PUBG VOD against owner labels — fast path for owner timecode drops."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from eval_owner_labels import eval_vod, load_videos


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("video_id")
    parser.add_argument("--good-tol", type=float, default=18.0)
    parser.add_argument("--bad-tol", type=float, default=12.0)
    parser.add_argument("--out", default="")
    args = parser.parse_args()

    os.environ.setdefault("HIGHLIGHT_INBOX", "/root/data/pubg/youtube_nightly/inbox")
    os.environ.setdefault("HIGHLIGHT_USE_OWNER_ANCHORS", "0")
    os.environ.setdefault("PUBG_OWNER_ANCHORS", "0")
    os.environ.setdefault("PUBG_SOFT_ANCHOR", "0")
    os.environ.setdefault("HIGHLIGHT_SOFT_ANCHOR", "0")
    os.environ.setdefault("HIGHLIGHT_HEATMAP", "0")

    videos = load_videos("pubg")
    rows = videos.get(args.video_id, [])
    if not rows:
        print(f"no labels for {args.video_id}")
        return 1
    row = eval_vod("pubg", args.video_id, rows, good_tol=args.good_tol, bad_tol=args.bad_tol)
    print(json.dumps(row, indent=2, ensure_ascii=False))
    misses = [p for p in row.get("good_detail", "").split(",") if p.startswith("MISS")]
    print(f"\nrecall={row.get('recall',0):.0%} missed={len(misses)}")
    if misses:
        print("miss_list:", " ".join(misses))
    if args.out:
        Path(args.out).write_text(json.dumps(row, indent=2, ensure_ascii=False), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
