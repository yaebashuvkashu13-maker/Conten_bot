#!/usr/bin/env python3
"""CLI: remove watermark from a local image (for VPS/manual checks)."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from image_watermark_remove import clean_image_file, find_watermark_boxes, remove_watermarks

import cv2


def main() -> int:
    parser = argparse.ArgumentParser(description="Remove IG watermark phrases from an image")
    parser.add_argument("image", type=Path, help="Input image path")
    parser.add_argument("-o", "--output", type=Path, help="Output path (default: <name>_clean.jpg)")
    parser.add_argument("--boxes-only", action="store_true", help="Print detected boxes, do not write")
    args = parser.parse_args()

    img = cv2.imread(str(args.image))
    if img is None:
        print(f"cannot read {args.image}", file=sys.stderr)
        return 1

    boxes = find_watermark_boxes(img)
    print(f"boxes={boxes}")
    if args.boxes_only:
        return 0 if boxes else 2

    out_path, changed = clean_image_file(args.image)
    if not changed:
        print("no watermark detected")
        return 2

    dest = args.output or out_path
    if dest != out_path:
        import shutil

        shutil.copy2(out_path, dest)
    print(f"saved {dest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
