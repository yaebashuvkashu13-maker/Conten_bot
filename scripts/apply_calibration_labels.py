#!/usr/bin/env python3
"""Apply owner calibration labels (1-15): quarantine bad, whitelist good, build reject frames."""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

BATCH_STATE = Path("/root/data/mlbb/calibration_batch_sent.json")
LABELS_PATH = Path("/root/data/mlbb/calibration_labels.json")
GOOD_PATH = Path("/root/data/mlbb/calibration_good_sources.json")
QUARANTINE_ROOT = Path("/root/data/mlbb/quarantine")
REJECT_DIR = Path("/root/data/mlbb/reject_examples")
HERO_ROOT = Path("/root/hero_datasets")

# Owner review 2026-06-03 (order matches send_mlbb_gameplay_calibration batch)
OWNER_LABELS: dict[int, str] = {
    1: "good",
    2: "promo",
    3: "interview",
    4: "memes",
    5: "ad",
    6: "good",
    7: "ad",
    8: "good",
    9: "good",
    10: "good",  # bad quality but gameplay
    11: "promo",
    12: "good",
    13: "promo",
    14: "promo",
    15: "cartoon",
}


def extract_reject_frames(video: Path, tag: str, count: int = 3) -> list[Path]:
    REJECT_DIR.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(video),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    try:
        duration = float((result.stdout or "30").strip())
    except ValueError:
        duration = 30.0
    saved: list[Path] = []
    for idx, frac in enumerate([0.2, 0.5, 0.75][:count], start=1):
        t = max(0.5, duration * frac)
        out = REJECT_DIR / f"reject_cal_{tag}_{video.stem}_{idx}.jpg"
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-ss",
                f"{t:.2f}",
                "-i",
                str(video),
                "-frames:v",
                "1",
                "-q:v",
                "2",
                str(out),
            ],
            capture_output=True,
            timeout=60,
        )
        if out.exists():
            saved.append(out)
    return saved


def quarantine_file(path: Path, reason: str) -> Path | None:
    if not path.exists():
        return None
    dest_dir = QUARANTINE_ROOT / reason
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / path.name
    if dest.exists():
        dest.unlink()
    path.rename(dest)
    return dest


def main() -> int:
    if not BATCH_STATE.exists():
        print(f"missing {BATCH_STATE}", flush=True)
        return 1
    batch = json.loads(BATCH_STATE.read_text())
    paths = [Path(p) for p in batch.get("paths", [])]
    if len(paths) != 15:
        print(f"expected 15 paths, got {len(paths)}", flush=True)

    good: list[dict] = []
    bad: list[dict] = []
    reject_frames = 0

    for num, path in enumerate(paths, start=1):
        label = OWNER_LABELS.get(num, "unknown")
        row = {"num": num, "path": str(path), "label": label, "hero": path.parent.name}
        if label == "good":
            good.append(row)
            continue
        bad.append(row)
        if path.exists():
            frames = extract_reject_frames(path, f"n{num}_{label}")
            reject_frames += len(frames)
            quarantine_file(path, label)
        else:
            print(f"[skip] missing {path}", flush=True)

    payload = {
        "labels": OWNER_LABELS,
        "good": good,
        "bad": bad,
        "at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    LABELS_PATH.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
    GOOD_PATH.write_text(json.dumps(good, indent=2, ensure_ascii=False))

    print(f"good={len(good)} bad={len(bad)} reject_frames={reject_frames}", flush=True)
    for row in good:
        print(f"  OK #{row['num']} {row['hero']} {Path(row['path']).name}", flush=True)
    for row in bad:
        print(f"  OUT #{row['num']} {row['label']} {row['hero']}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
