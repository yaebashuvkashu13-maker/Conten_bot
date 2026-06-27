#!/usr/bin/env python3
"""Bulk-import owner time labels into pubg_owner_labels.json."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from pubg_owner_learning import append_owner_time_label, load_owner_labels_json, save_owner_labels_json


def parse_tc(raw: str) -> float:
    raw = raw.strip()
    if ":" in raw:
        parts = raw.split(":")
        if len(parts) == 2:
            return float(int(parts[0]) * 60 + int(parts[1]))
        if len(parts) == 3:
            return float(int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2]))
    return float(raw)


def import_video_labels(
    video_id: str,
    times: list[float],
    *,
    label: str = "good",
    note: str = "owner_metro_kill",
    source: str = "owner",
) -> int:
    added = 0
    for t in times:
        if append_owner_time_label(video_id, t, label, note=note, source=source):
            added += 1
    return added


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("video_id")
    parser.add_argument("times", nargs="*", help="273 or 4:33")
    parser.add_argument("--file", default="", help="JSON file with videos dict")
    parser.add_argument("--label", default="good")
    parser.add_argument("--note", default="owner_metro_kill")
    args = parser.parse_args()

    if args.file:
        data = json.loads(Path(args.file).read_text(encoding="utf-8"))
        rows = data.get("videos", {}).get(args.video_id, [])
        times = [float(r["time_sec"]) for r in rows if r.get("label") == args.label]
    else:
        times = [parse_tc(t) for t in args.times]

    added = import_video_labels(
        args.video_id,
        sorted(set(times)),
        label=args.label,
        note=args.note,
    )
    print(f"imported {added} new anchors for {args.video_id} (total labels in file updated)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
