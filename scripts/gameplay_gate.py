#!/usr/bin/env python3
"""Quick gameplay vs promo/memes check for MLBB TikTok clips."""

from __future__ import annotations

import argparse
import csv
import re
from pathlib import Path

import cv2
import numpy as np

PROMO_PATTERNS = re.compile(
    r"(#ad\b|sponsored|giveaway|promo\b|free\s+diamond|skin\s+gratis|"
    r"log\s*in\s+mlbb|mailbox|click\s+link|download\s+now|official\s+event)",
    re.I,
)


def load_csv_lookup(csv_path: Path) -> dict[str, bool]:
    if not csv_path.exists():
        return {}
    lookup: dict[str, bool] = {}
    with csv_path.open(encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            video_id = str(row.get("video_id") or "").strip()
            if not video_id:
                continue
            raw = str(row.get("is_gameplay", row.get("gameplay_score", ""))).strip().lower()
            if raw in {"true", "1", "yes"}:
                lookup[video_id] = True
            elif raw in {"false", "0", "no"}:
                lookup[video_id] = False
            else:
                try:
                    lookup[video_id] = float(raw) >= 0.85
                except ValueError:
                    pass
    return lookup


def extract_video_id(path: Path, description: str = "") -> str | None:
    text = f"{path.name} {description} {path}"
    match = re.search(r"(\d{10,22})", text)
    return match.group(1) if match else None


def heuristic_gameplay_score(video_path: Path, sample_frames: int = 4) -> float:
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        return 0.0
    frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    if frame_count <= 0:
        cap.release()
        return 0.0
    indices = np.linspace(0, max(frame_count - 1, 0), num=min(sample_frames, frame_count), dtype=int)
    hud_scores: list[float] = []
    motion_scores: list[float] = []
    prev_gray = None
    for idx in indices:
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(idx))
        ok, frame = cap.read()
        if not ok:
            continue
        small = cv2.resize(frame, (160, 90))
        gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
        h, w = gray.shape
        minimap = gray[int(h * 0.72) : h, 0 : int(w * 0.28)]
        skill_bar = gray[int(h * 0.72) : h, int(w * 0.55) : w]
        top_hud = gray[0 : int(h * 0.16), :]
        hud_scores.append(float(np.std(minimap) + np.std(skill_bar) + np.std(top_hud) * 0.5))
        if prev_gray is not None:
            motion_scores.append(float(cv2.absdiff(gray, prev_gray).mean() / 255.0))
        prev_gray = gray
    cap.release()
    if not hud_scores:
        return 0.0
    hud = float(np.mean(hud_scores))
    motion = float(np.mean(motion_scores)) if motion_scores else 0.0
    return min(1.0, hud / 28.0 * 0.75 + motion * 0.25)


def is_gameplay_video(
    video_path: Path,
    *,
    csv_lookup: dict[str, bool],
    description: str = "",
    min_score: float = 0.72,
) -> tuple[bool, float, str]:
    if PROMO_PATTERNS.search(description):
        return False, 0.0, "promo_text"

    video_id = extract_video_id(video_path, description)
    if video_id and video_id in csv_lookup:
        known = csv_lookup[video_id]
        return known, 1.0 if known else 0.0, "csv_lookup"

    score = heuristic_gameplay_score(video_path)
    return score >= min_score, score, "heuristic"


def main() -> int:
    parser = argparse.ArgumentParser(description="Check if a video looks like MLBB gameplay.")
    parser.add_argument("video", type=Path)
    parser.add_argument("--csv", type=Path, default=Path("/root/data/mlbb/gameplay_filter_latest.csv"))
    parser.add_argument("--description", default="")
    args = parser.parse_args()
    lookup = load_csv_lookup(args.csv)
    ok, score, reason = is_gameplay_video(args.video, csv_lookup=lookup, description=args.description)
    print(f"gameplay={ok} score={score:.3f} reason={reason}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
