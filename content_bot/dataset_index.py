from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path


def infer_label(video_path: Path, labeled_root: Path | None) -> str | None:
    if labeled_root is None:
        return None
    try:
        relative = video_path.relative_to(labeled_root)
    except ValueError:
        return None
    parts = relative.parts
    if len(parts) < 2:
        return None
    # datasets/labeled/<hero>/file.mp4 -> hero
    # datasets/labeled/<hero>/<skin>/file.mp4 -> hero/skin
    if len(parts) == 2:
        return parts[0]
    return f"{parts[0]}/{parts[1]}"


def index_videos(
    root: Path,
    *,
    labeled_root: Path | None = None,
) -> list[dict]:
    rows: list[dict] = []
    for video_path in sorted(root.rglob("*.mp4")):
        stat = video_path.stat()
        rows.append(
            {
                "path": str(video_path.resolve()),
                "size_bytes": stat.st_size,
                "mtime": stat.st_mtime,
                "label": infer_label(video_path, labeled_root),
            }
        )
    return rows


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Index local .mp4 files into manifest CSV/JSON.")
    parser.add_argument("--root", default="datasets", help="Root directory to scan.")
    parser.add_argument("--labeled-root", default="datasets/labeled", help="Folder used to infer labels.")
    parser.add_argument("--output-csv", default="datasets/index/videos.csv")
    parser.add_argument("--output-json", default="datasets/index/videos.json")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    root = Path(args.root)
    labeled_root = Path(args.labeled_root) if args.labeled_root else None
    if not root.exists():
        print(f"Root not found: {root}")
        return 1

    rows = index_videos(root, labeled_root=labeled_root if labeled_root and labeled_root.exists() else None)
    if not rows:
        print(f"No .mp4 files under {root}")
        return 0

    csv_path = Path(args.output_csv)
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)

    json_path = Path(args.output_json)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")

    labeled = sum(1 for row in rows if row.get("label"))
    print(f"Indexed {len(rows)} videos ({labeled} with labels) -> {csv_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
