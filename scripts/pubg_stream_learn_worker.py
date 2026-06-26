#!/usr/bin/env python3
"""Background PUBG stream dataset: copy uploads + append motion/HUD features."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import shutil
import time
from pathlib import Path

import cv2
import numpy as np

DATASET_DIR = Path("/root/datasets/telegram/pubg/stream")
FEATURES_CSV = Path("/root/data/pubg/stream_features.csv")
STATE_PATH = Path("/root/data/pubg/stream_learn_state.json")
FIELDS = [
    "video_id",
    "path",
    "chat_id",
    "duration_sec",
    "motion_mean",
    "motion_max",
    "center_motion_mean",
    "sharpness_mean",
    "saved_at",
]


def file_id(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()[:16]


def extract_features(video_path: Path, sample_fps: float = 3.0) -> dict[str, float]:
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        raise RuntimeError(f"cannot open {video_path}")
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    step = max(int(round(fps / sample_fps)), 1)
    motion: list[float] = []
    center_motion: list[float] = []
    sharpness: list[float] = []
    prev_gray = None
    idx = 0
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        if idx % step != 0:
            idx += 1
            continue
        small = cv2.resize(frame, (160, 90))
        gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
        h, w = gray.shape
        y0, y1 = int(h * 0.2), int(h * 0.8)
        x0, x1 = int(w * 0.15), int(w * 0.85)
        center = gray[y0:y1, x0:x1]
        sharpness.append(float(cv2.Laplacian(gray, cv2.CV_32F).var()))
        if prev_gray is not None:
            motion.append(float(cv2.absdiff(gray, prev_gray).mean() / 255.0))
            center_motion.append(float(cv2.absdiff(center, prev_gray[y0:y1, x0:x1]).mean() / 255.0))
        prev_gray = gray
        idx += 1
    cap.release()
    duration = idx / fps if fps else 0.0

    def mean_max(vals: list[float], prefix: str) -> dict[str, float]:
        if not vals:
            return {f"{prefix}_mean": 0.0, f"{prefix}_max": 0.0}
        arr = np.array(vals, dtype=np.float32)
        return {f"{prefix}_mean": float(arr.mean()), f"{prefix}_max": float(arr.max())}

    out = {"duration_sec": float(duration)}
    out.update(mean_max(motion, "motion"))
    out.update(mean_max(center_motion, "center_motion"))
    out["sharpness_mean"] = float(np.mean(sharpness)) if sharpness else 0.0
    return out


def load_state() -> dict:
    if not STATE_PATH.exists():
        return {"indexed_ids": []}
    try:
        return json.loads(STATE_PATH.read_text())
    except json.JSONDecodeError:
        return {"indexed_ids": []}


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    state["indexed_ids"] = list(state.get("indexed_ids", []))[-5000:]
    state["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2))


def append_csv(row: dict) -> None:
    FEATURES_CSV.parent.mkdir(parents=True, exist_ok=True)
    new_file = not FEATURES_CSV.exists()
    with FEATURES_CSV.open("a", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        if new_file:
            writer.writeheader()
        writer.writerow({key: row.get(key, "") for key in FIELDS})


def ingest(video: Path, chat_id: str = "") -> dict:
    vid = file_id(video)
    state = load_state()
    if vid in state.get("indexed_ids", []):
        return {"status": "skip", "video_id": vid}

    DATASET_DIR.mkdir(parents=True, exist_ok=True)
    dest = DATASET_DIR / f"{vid}.mp4"
    if not dest.exists():
        shutil.copy2(video, dest)

    feats = extract_features(dest)
    row = {
        "video_id": vid,
        "path": str(dest),
        "chat_id": chat_id,
        "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        **feats,
    }
    append_csv(row)
    state.setdefault("indexed_ids", []).append(vid)
    save_state(state)
    return {"status": "ok", "video_id": vid, **feats}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--video", type=Path, required=True)
    parser.add_argument("--chat-id", default="")
    args = parser.parse_args()
    if not args.video.exists():
        print(json.dumps({"status": "error", "reason": "missing file"}))
        return 1
    try:
        result = ingest(args.video, args.chat_id)
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result.get("status") == "ok" else 0
    except Exception as exc:
        print(json.dumps({"status": "error", "reason": str(exc)}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
