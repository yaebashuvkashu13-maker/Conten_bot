#!/usr/bin/env python3
"""Quick gameplay vs promo/memes check for MLBB TikTok clips."""

from __future__ import annotations

import argparse
import csv
import re
from pathlib import Path

import cv2
import numpy as np

REJECT_EXAMPLES_DIR = Path("/root/data/mlbb/reject_examples")
_REJECT_REF_HISTS: list[np.ndarray] | None = None

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


def _frame_hud_metrics(frame: np.ndarray) -> tuple[float, float, float]:
    small = cv2.resize(frame, (160, 90))
    gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
    h, w = gray.shape
    minimap = gray[int(h * 0.72) : h, 0 : int(w * 0.28)]
    skill_bar = gray[int(h * 0.72) : h, int(w * 0.55) : w]
    top_hud = gray[0 : int(h * 0.16), :]
    return float(np.std(minimap)), float(np.std(skill_bar)), float(np.std(top_hud))


def _frame_overlay_text_score(frame: np.ndarray) -> float:
    """Higher = more likely subtitles / meme text in the center of the frame."""
    small = cv2.resize(frame, (320, 180))
    gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
    h, w = gray.shape
    band = gray[int(h * 0.32) : int(h * 0.70), int(w * 0.08) : int(w * 0.92)]
    if band.size == 0:
        return 0.0
    edges = cv2.Canny(band, 70, 170)
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (max(w // 10, 11), 3))
    merged = cv2.morphologyEx(edges, cv2.MORPH_CLOSE, kernel)
    edge_ratio = float(np.count_nonzero(merged)) / float(merged.size)
    _, bright = cv2.threshold(band, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    bright_ratio = float(np.count_nonzero(bright)) / float(bright.size)
    return edge_ratio * 0.7 + min(bright_ratio, 0.35) * 0.3


def score_segment_window(
    video_path: Path,
    start_sec: float,
    duration_sec: float,
    *,
    sample_frames: int = 5,
) -> tuple[float, float, float]:
    """Returns (hud_score, text_score, cartoon_penalty). Higher hud = more like MLBB UI."""
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        return 0.0, 1.0, 1.0
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    end_sec = start_sec + max(duration_sec, 0.5)
    times = np.linspace(start_sec, max(start_sec + 0.1, end_sec - 0.05), num=sample_frames)
    hud_vals: list[float] = []
    text_vals: list[float] = []
    low_hud_frames = 0
    for t in times:
        cap.set(cv2.CAP_PROP_POS_MSEC, float(t) * 1000.0)
        ok, frame = cap.read()
        if not ok:
            continue
        mini, skill, top = _frame_hud_metrics(frame)
        hud_vals.append(mini + skill + top * 0.6)
        text_vals.append(_frame_overlay_text_score(frame))
        if mini < 6.0 and skill < 5.5:
            low_hud_frames += 1
    cap.release()
    if not hud_vals:
        return 0.0, 1.0, 1.0
    hud = float(np.mean(hud_vals))
    text = float(np.mean(text_vals))
    cartoon_penalty = low_hud_frames / max(len(hud_vals), 1)
    return hud, text, cartoon_penalty


def _reject_reference_histograms() -> list[np.ndarray]:
    global _REJECT_REF_HISTS
    if _REJECT_REF_HISTS is not None:
        return _REJECT_REF_HISTS
    hists: list[np.ndarray] = []
    if REJECT_EXAMPLES_DIR.exists():
        for path in sorted(REJECT_EXAMPLES_DIR.glob("reject_*.*")):
            if path.suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp"}:
                continue
            img = cv2.imread(str(path))
            if img is None:
                continue
            hists.append(_center_band_hist(img))
    _REJECT_REF_HISTS = hists
    return hists


def _center_band_hist(frame: np.ndarray) -> np.ndarray:
    small = cv2.resize(frame, (320, 180))
    band = small[int(180 * 0.28) : int(180 * 0.72), int(320 * 0.08) : int(320 * 0.92)]
    if band.size == 0:
        return np.zeros((1, 1), dtype=np.float32)
    hsv = cv2.cvtColor(band, cv2.COLOR_BGR2HSV)
    hist = cv2.calcHist([hsv], [0, 1], None, [24, 16], [0, 180, 0, 256])
    cv2.normalize(hist, hist)
    return hist.astype(np.float32)


def reject_example_similarity(video_path: Path, start_sec: float, duration_sec: float) -> float:
    """0..1 — higher means frame looks like owner /bad examples."""
    refs = _reject_reference_histograms()
    if not refs:
        return 0.0
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        return 0.0
    end_sec = start_sec + max(duration_sec, 0.5)
    times = np.linspace(start_sec, max(start_sec + 0.1, end_sec - 0.05), num=3)
    best = 0.0
    for t in times:
        cap.set(cv2.CAP_PROP_POS_MSEC, float(t) * 1000.0)
        ok, frame = cap.read()
        if not ok:
            continue
        hist = _center_band_hist(frame)
        for ref in refs:
            score = float(cv2.compareHist(hist, ref, cv2.HISTCMP_CORREL))
            best = max(best, score)
    cap.release()
    return best


def segment_is_valid_for_montage(
    video_path: Path,
    start_sec: float,
    duration_sec: float,
    *,
    profile: str = "mobile_legends",
    min_hud: float = 14.0,
    max_text: float = 0.075,
    max_cartoon_ratio: float = 0.55,
    max_reject_similarity: float = 0.82,
) -> tuple[bool, str]:
    if profile != "mobile_legends":
        return True, "skip_profile"
    hud, text, cartoon = score_segment_window(video_path, start_sec, duration_sec)
    reject_sim = reject_example_similarity(video_path, start_sec, duration_sec)
    if reject_sim >= max_reject_similarity:
        return False, f"reject_example_sim={reject_sim:.2f}"
    if cartoon >= max_cartoon_ratio and hud < min_hud * 1.05:
        return False, f"non_gameplay_hud={hud:.1f}"
    if hud < min_hud:
        return False, f"low_hud={hud:.1f}"
    if text > max_text:
        return False, f"overlay_text={text:.3f}"
    return True, "ok"


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
        mini, skill, top = _frame_hud_metrics(frame)
        hud_scores.append(mini + skill + top * 0.5)
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
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
