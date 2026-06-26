#!/usr/bin/env python3
"""Extract video features from new MLBB downloads (overnight training)."""

from __future__ import annotations

import csv
import hashlib
import json
import sys
import time
from pathlib import Path

import cv2
import numpy as np

INBOX = Path("/root/datasets/tiktok/mlbb")
OUT_CSV = Path("/root/data/mlbb/video_features_all.csv")
STATE = Path("/root/data/mlbb/feature_batch_state.json")
BATCH = 25


def file_id(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def extract_features(video_path: Path) -> dict[str, float]:
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        raise RuntimeError(f"cannot open {video_path}")
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    step = max(int(round(fps / 3.0)), 1)
    motion: list[float] = []
    prev = None
    idx = 0
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        if idx % step == 0:
            gray = cv2.cvtColor(cv2.resize(frame, (160, 90)), cv2.COLOR_BGR2GRAY)
            if prev is not None:
                motion.append(float(cv2.absdiff(gray, prev).mean() / 255.0))
            prev = gray
        idx += 1
    cap.release()
    arr = np.array(motion, dtype=np.float32) if motion else np.zeros(1)
    return {
        "duration_sec": float(idx / fps) if fps else 0.0,
        "motion_mean": float(arr.mean()),
        "motion_max": float(arr.max()),
    }


def main() -> int:
    done = set()
    if STATE.exists():
        done = set(json.loads(STATE.read_text()).get("done", []))
    rows: list[dict] = []
    if OUT_CSV.exists():
        with OUT_CSV.open(encoding="utf-8") as f:
            for row in csv.DictReader(f):
                rows.append(row)
                done.add(row.get("video_id", ""))

    added = 0
    for video in sorted(INBOX.rglob("*.mp4")):
        if "non_gameplay" in video.parts:
            continue
        vid = file_id(video)
        if vid in done or added >= BATCH:
            continue
        try:
            feats = extract_features(video)
        except Exception as exc:
            print(json.dumps({"skip": str(video), "err": str(exc)}))
            continue
        rows.append({"video_id": vid, "path": str(video), **feats, "extracted_at": time.strftime("%Y-%m-%d %H:%M:%S")})
        done.add(vid)
        added += 1

    if added:
        OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
        fields = list(rows[0].keys())
        with OUT_CSV.open("w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            w.writerows(rows)
    STATE.write_text(json.dumps({"done": sorted(done)[-8000:], "updated": time.strftime("%Y-%m-%d %H:%M:%S")}, indent=2))
    print(json.dumps({"added": added, "total": len(rows)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
