from __future__ import annotations

import argparse
import csv
from pathlib import Path

from .dataset_index import index_videos
from .video_features import extract_video_features


def extract_for_videos(
    videos: list[dict],
    *,
    label_column: str = "label",
    default_label: str = "unknown",
    limit: int | None = None,
) -> list[dict]:
    rows: list[dict] = []
    selected = videos[:limit] if limit else videos
    total = len(selected)

    for index, video in enumerate(selected, start=1):
        path = Path(video["path"])
        label = video.get(label_column) or default_label
        print(f"[{index}/{total}] {path.name} ({label})")
        try:
            features = extract_video_features(path)
        except Exception as exc:
            print(f"  skip: {exc}")
            continue
        rows.append({"path": str(path), "label": label, **features})
    return rows


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Extract video features for many local .mp4 files.")
    parser.add_argument("--root", default="datasets")
    parser.add_argument("--labeled-root", default="datasets/labeled")
    parser.add_argument("--from-index", help="Use existing CSV from dataset_index instead of scanning.")
    parser.add_argument("--output-csv", default="datasets/features/all.csv")
    parser.add_argument("--limit", type=int, help="Process only first N videos (smoke test).")
    parser.add_argument("--default-label", default="unknown")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.from_index:
        index_path = Path(args.from_index)
        with index_path.open(encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            videos = list(reader)
    else:
        labeled_root = Path(args.labeled_root) if args.labeled_root else None
        videos = index_videos(
            Path(args.root),
            labeled_root=labeled_root if labeled_root and labeled_root.exists() else None,
        )

    if not videos:
        print("No videos to process.")
        return 1

    rows = extract_for_videos(
        videos,
        default_label=args.default_label,
        limit=args.limit,
    )
    if not rows:
        print("No features extracted.")
        return 1

    output_path = Path(args.output_csv)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)

    print(f"Wrote {len(rows)} feature rows -> {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
