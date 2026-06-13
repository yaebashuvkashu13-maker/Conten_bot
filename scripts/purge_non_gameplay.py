#!/usr/bin/env python3
"""Remove TikTok clips without real MLBB gameplay from dataset tree."""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

from gameplay_gate import is_gameplay_video, load_csv_lookup

DEFAULT_ROOT = Path("/root/datasets/tiktok/mlbb")
DEFAULT_CSV = Path("/root/data/mlbb/gameplay_filter_latest.csv")


def main() -> int:
    parser = argparse.ArgumentParser(description="Delete non-gameplay MLBB TikTok clips")
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--csv", type=Path, default=DEFAULT_CSV)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    lookup = load_csv_lookup(args.csv)
    removed = 0
    kept = 0

    ng = args.root / "non_gameplay"
    if ng.exists():
        count = sum(1 for _ in ng.rglob("*.mp4"))
        if args.dry_run:
            print(f"would remove non_gameplay/: {count} mp4")
            removed += count
        else:
            shutil.rmtree(ng)
            print(f"removed non_gameplay/: {count} mp4")
            removed += count

    for mp4 in sorted(args.root.rglob("*.mp4")):
        if "non_gameplay" in mp4.parts:
            continue
        ok, _score, reason = is_gameplay_video(mp4, csv_lookup=lookup)
        if ok:
            kept += 1
            continue
        removed += 1
        if args.dry_run:
            print(f"would delete {mp4} ({reason})")
        else:
            mp4.unlink(missing_ok=True)
            parent = mp4.parent
            if parent != args.root and not any(parent.iterdir()):
                parent.rmdir()

    print(f"done removed={removed} kept={kept} dry_run={args.dry_run}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
