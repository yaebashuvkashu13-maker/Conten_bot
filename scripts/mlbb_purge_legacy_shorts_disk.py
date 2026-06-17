#!/usr/bin/env python3
"""Remove legacy Shorts junk from disk + index (stubs, wrong-game, unlabeled orphans).

Keeps owner 👍 YouTube Shorts (11-char video_id). Deletes mp4 for everything else.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mlbb_calibration_store import (
    SHORTS_ROOT,
    _expected_path,
    id_from_path,
    is_stub_candidate,
    load_index,
    load_labels,
    save_index,
)


def owner_keep_youtube_ids() -> set[str]:
    """video_ids owner marked 👍 (YouTube Shorts only)."""
    labels = load_labels()
    keep: set[str] = set()
    for row in labels.get("feedback", []):
        vid = str(row.get("video_id") or row.get("id") or "").strip()
        if len(vid) != 11:
            continue
        if row.get("owner_label") in ("yes", "good"):
            keep.add(vid)
    for row in labels.get("good", []):
        vid = str(row.get("video_id") or row.get("id") or "").strip()
        path = Path(str(row.get("path", "")))
        if path.name.startswith("yt_"):
            vid = id_from_path(path)
        if len(vid) == 11:
            keep.add(vid)
    return keep


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Print actions only")
    parser.add_argument(
        "--delete-mp4",
        action="store_true",
        default=os.environ.get("MLBB_PURGE_DELETE_MP4", "1") == "1",
        help="Delete mp4 files (default on)",
    )
    args = parser.parse_args()

    keep = owner_keep_youtube_ids()
    data = load_index()
    rows = data.get("candidates", [])
    stub_rows = sum(1 for r in rows if is_stub_candidate(r))
    disk = sorted(SHORTS_ROOT.glob("yt_*.mp4")) if SHORTS_ROOT.exists() else []

    delete_vids: list[str] = []
    for mp4 in disk:
        vid = id_from_path(mp4)
        if vid in keep:
            continue
        delete_vids.append(vid)

    new_rows = []
    drop_index = 0
    for row in rows:
        vid = str(row.get("video_id") or "").strip()
        if vid in keep and not is_stub_candidate(row):
            new_rows.append(row)
            continue
        if vid in keep and is_stub_candidate(row):
            # Owner 👍 but legacy metadata — drop from send queue (file kept on disk).
            drop_index += 1
            continue
        drop_index += 1

    print(
        f"legacy_purge keep={len(keep)} disk={len(disk)} delete_mp4={len(delete_vids)} "
        f"index_drop={drop_index} stub_rows={stub_rows} dry_run={args.dry_run}",
        flush=True,
    )

    if args.dry_run:
        for vid in delete_vids[:20]:
            print(f"  delete {vid}", flush=True)
        if len(delete_vids) > 20:
            print(f"  ... and {len(delete_vids) - 20} more", flush=True)
        return 0

    deleted = 0
    freed = 0
    for vid in delete_vids:
        path = _expected_path(vid)
        if not path.exists():
            continue
        try:
            freed += path.stat().st_size
            if args.delete_mp4:
                path.unlink()
                deleted += 1
        except OSError as exc:
            print(f"WARN delete {vid}: {exc}", flush=True)

    data["candidates"] = new_rows
    data["legacy_purge_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    data["legacy_purge_deleted"] = deleted
    save_index(data)

    print(
        f"SUMMARY deleted_mp4={deleted} freed_mb={freed / (1024 * 1024):.1f} "
        f"index_rows={len(new_rows)} kept_owner_good={len(keep)}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
