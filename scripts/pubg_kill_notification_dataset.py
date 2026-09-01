#!/usr/bin/env python3
"""Collect and label PUBG kill-notification crops for classifier training."""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any

import cv2
import numpy as np

DEFAULT_ROOT = "/root/data/pubg/kill_notification_crops"
MANIFEST = "manifest.jsonl"
LABELS = (
    "kill",
    "knock",
    "teammate_kill",
    "author_death",
    "hud_fp",
    "map_blue",
    "low_res",
    "moved_hud",
    "uncertain",
)


def dataset_root() -> Path:
    return Path(os.environ.get("PUBG_KILL_NOTIFICATION_DATASET", DEFAULT_ROOT))


def manifest_path() -> Path:
    return dataset_root() / MANIFEST


def save_enabled() -> bool:
    return os.environ.get("PUBG_KILL_NOTIFICATION_SAVE_CROPS", "1") == "1"


def _crop_key(video_path: Path, start_sec: float, box: list[float] | None) -> str:
    stat = video_path.stat()
    blob = f"{video_path.resolve()}|{stat.st_mtime_ns}|{start_sec:.2f}|{box}"
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def save_crop(
    crop: np.ndarray,
    *,
    video_path: Path,
    start_sec: float,
    box: list[float] | None,
    score: float,
    text: str = "",
    label: str = "uncertain",
    meta: dict[str, Any] | None = None,
) -> str | None:
    if not save_enabled() or crop is None or crop.size == 0:
        return None
    root = dataset_root()
    root.mkdir(parents=True, exist_ok=True)
    key = _crop_key(video_path, start_sec, box)
    out = root / f"{key}.jpg"
    if out.is_file():
        return key
    ok = cv2.imwrite(str(out), crop)
    if not ok:
        return None
    row = {
        "key": key,
        "path": str(out),
        "video": video_path.name,
        "start_sec": round(float(start_sec), 2),
        "score": round(float(score), 4),
        "text": (text or "")[:120],
        "label": label if label in LABELS else "uncertain",
        "box": box,
        "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    if meta:
        row["meta"] = meta
    with manifest_path().open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    return key


def extract_crop(frame: np.ndarray, box: list[float] | None) -> np.ndarray | None:
    if frame is None or frame.size == 0 or not box:
        return None
    h, w = frame.shape[:2]
    x, y, bw, bh = (int(v) for v in box)
    x = max(0, min(x, w - 1))
    y = max(0, min(y, h - 1))
    bw = max(1, min(bw, w - x))
    bh = max(1, min(bh, h - y))
    return frame[y : y + bh, x : x + bw].copy()


def load_manifest() -> list[dict[str, Any]]:
    path = manifest_path()
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def update_label(key: str, label: str, *, note: str = "") -> bool:
    if label not in LABELS:
        return False
    rows = load_manifest()
    updated = False
    for row in rows:
        if row.get("key") == key:
            row["label"] = label
            if note:
                row["note"] = note[:200]
            row["labeled_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            updated = True
    if not updated:
        return False
    manifest_path().write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
        encoding="utf-8",
    )
    return True


__all__ = ["LABELS", "extract_crop", "load_manifest", "save_crop", "update_label"]
