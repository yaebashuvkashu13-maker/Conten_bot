#!/usr/bin/env python3
"""Audit PUBG owner labels — counts, reasons, holdout by channel."""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from runtime_labels import load_runtime_labels


def audit(labels: dict) -> dict:
    videos = labels.get("videos") or {}
    label_counts: Counter[str] = Counter()
    reason_counts: Counter[str] = Counter()
    source_counts: Counter[str] = Counter()
    per_video: dict[str, dict] = {}
    for video_id, rows in videos.items():
        good = bad = uncertain = 0
        for row in rows:
            label = str(row.get("label") or "unknown")
            label_counts[label] += 1
            if row.get("note"):
                reason_counts[str(row["note"])[:60]] += 1
            source_counts[str(row.get("source") or "owner")] += 1
            if label == "good":
                good += 1
            elif label == "bad":
                bad += 1
            elif label == "uncertain":
                uncertain += 1
        per_video[video_id] = {"good": good, "bad": bad, "uncertain": uncertain, "total": len(rows)}
    return {
        "video_count": len(videos),
        "label_counts": dict(label_counts),
        "reason_counts": dict(reason_counts.most_common(20)),
        "source_counts": dict(source_counts),
        "per_video": per_video,
        "updated_at": labels.get("updated_at"),
        "seeded_from": labels.get("seeded_from"),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = audit(load_runtime_labels("pubg"))
    text = json.dumps(report, indent=2, ensure_ascii=False)
    print(text)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
