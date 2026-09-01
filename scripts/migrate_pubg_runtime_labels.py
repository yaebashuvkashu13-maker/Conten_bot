#!/usr/bin/env python3
"""Move mutable PUBG owner labels out of the production git checkout."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path


def _read(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def merge_labels(paths: list[Path]) -> dict:
    videos: dict[str, list[dict]] = {}
    seen: dict[str, set[tuple[float, str, str]]] = {}
    for path in paths:
        data = _read(path)
        source_videos = data.get("videos") if isinstance(data.get("videos"), dict) else data
        if not isinstance(source_videos, dict):
            continue
        for video_id, rows in source_videos.items():
            if not isinstance(rows, list):
                continue
            videos.setdefault(str(video_id), [])
            seen.setdefault(str(video_id), set())
            for row in rows:
                if not isinstance(row, dict) or "time_sec" not in row:
                    continue
                key = (
                    round(float(row["time_sec"]), 2),
                    str(row.get("label") or ""),
                    str(row.get("source") or ""),
                )
                if key in seen[str(video_id)]:
                    continue
                seen[str(video_id)].add(key)
                videos[str(video_id)].append(dict(row))
    for rows in videos.values():
        rows.sort(key=lambda row: (float(row.get("time_sec", 0)), str(row.get("label", ""))))
    return {"schema_version": 2, "videos": videos}


def atomic_write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        delete=False,
    ) as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        temp = Path(handle.name)
    os.replace(temp, path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--repo-labels",
        type=Path,
        default=Path("/root/content_bot_ml/data/pubg_owner_labels.json"),
    )
    parser.add_argument(
        "--legacy-labels",
        type=Path,
        default=Path("/root/data/mlbb/pubg_owner_labels.json"),
    )
    parser.add_argument(
        "--runtime-labels",
        type=Path,
        default=Path("/root/data/pubg/pubg_owner_labels.json"),
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    merged = merge_labels([args.repo_labels, args.legacy_labels, args.runtime_labels])
    rows = sum(len(value) for value in merged["videos"].values())
    report = {
        "runtime_path": str(args.runtime_labels),
        "videos": len(merged["videos"]),
        "labels": rows,
        "dry_run": args.dry_run,
    }
    if not args.dry_run:
        atomic_write(args.runtime_labels, merged)
    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
