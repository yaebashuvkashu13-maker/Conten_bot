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


def _read_frame_at(cap: cv2.VideoCapture, t_sec: float) -> np.ndarray | None:
    cap.set(cv2.CAP_PROP_POS_MSEC, float(t_sec) * 1000.0)
    ok, frame = cap.read()
    return frame if ok else None


def detect_vertical_content_crop(
    video_path: Path,
    start_sec: float,
    duration_sec: float,
    *,
    sample_frames: int = 6,
) -> tuple[int, int, int, int] | None:
    """Crop static TikTok header/footer bands; return (x, y, w, h) in source pixels."""
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        return None
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
    if width < 120 or height < 200:
        cap.release()
        return None
    end_sec = start_sec + max(duration_sec, 0.5)
    times = np.linspace(start_sec, max(start_sec + 0.1, end_sec - 0.05), num=sample_frames)
    frames: list[np.ndarray] = []
    for t in times:
        frame = _read_frame_at(cap, float(t))
        if frame is not None:
            frames.append(frame)
    cap.release()
    if len(frames) < 2:
        return None

    small_h = 180
    small_w = max(72, int(width * small_h / height))
    row_activity = np.zeros(small_h, dtype=np.float32)
    for idx in range(1, len(frames)):
        prev = cv2.resize(frames[idx - 1], (small_w, small_h))
        curr = cv2.resize(frames[idx], (small_w, small_h))
        diff = cv2.absdiff(cv2.cvtColor(prev, cv2.COLOR_BGR2GRAY), cv2.cvtColor(curr, cv2.COLOR_BGR2GRAY))
        row_activity += diff.mean(axis=1)

    row_activity /= max(len(frames) - 1, 1)
    peak = float(np.percentile(row_activity, 90))
    if peak < 1.2:
        return None
    threshold = max(1.8, peak * 0.22)
    active = row_activity >= threshold
    if not np.any(active):
        return None
    indices = np.where(active)[0]
    top = int(indices[0])
    bottom = int(indices[-1])
    span = bottom - top + 1
    if span < int(small_h * 0.45):
        return None
    top_px = int(top * height / small_h)
    bottom_px = int((bottom + 1) * height / small_h)
    crop_h = bottom_px - top_px
    if crop_h < int(height * 0.50):
        return None
    trim_top = top_px / height
    trim_bottom = (height - bottom_px) / height
    if trim_top < 0.06 and trim_bottom < 0.06:
        return None
    return 0, top_px, width, crop_h


def score_segment_window(
    video_path: Path,
    start_sec: float,
    duration_sec: float,
    *,
    sample_frames: int = 5,
    crop_box: tuple[int, int, int, int] | None = None,
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
        frame = _read_frame_at(cap, float(t))
        if frame is None:
            continue
        if crop_box is not None:
            x, y, w, h = crop_box
            frame = frame[y : y + h, x : x + w]
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


def reject_example_similarity(
    video_path: Path,
    start_sec: float,
    duration_sec: float,
    *,
    crop_box: tuple[int, int, int, int] | None = None,
) -> float:
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
        frame = _read_frame_at(cap, float(t))
        if frame is None:
            continue
        if crop_box is not None:
            x, y, w, h = crop_box
            frame = frame[y : y + h, x : x + w]
        hist = _center_band_hist(frame)
        for ref in refs:
            score = float(cv2.compareHist(hist, ref, cv2.HISTCMP_CORREL))
            best = max(best, score)
    cap.release()
    return best


def segment_hud_frame_pass_rate(
    video_path: Path,
    start_sec: float,
    duration_sec: float,
    *,
    crop_box: tuple[int, int, int, int] | None = None,
    sample_frames: int = 5,
    min_minimap: float = 7.5,
    min_skill: float = 6.5,
) -> float:
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        return 0.0
    end_sec = start_sec + max(duration_sec, 0.5)
    times = np.linspace(start_sec, max(start_sec + 0.1, end_sec - 0.05), num=sample_frames)
    passed = 0
    total = 0
    for t in times:
        frame = _read_frame_at(cap, float(t))
        if frame is None:
            continue
        if crop_box is not None:
            x, y, w, h = crop_box
            frame = frame[y : y + h, x : x + w]
        mini, skill, _top = _frame_hud_metrics(frame)
        total += 1
        if mini >= min_minimap and skill >= min_skill:
            passed += 1
    cap.release()
    return passed / max(total, 1)


def segment_is_valid_for_montage(
    video_path: Path,
    start_sec: float,
    duration_sec: float,
    *,
    profile: str = "mobile_legends",
    min_hud: float = 14.0,
    max_text: float = 0.075,
    max_cartoon_ratio: float = 0.55,
    max_reject_similarity: float = 0.78,
    min_hud_frame_rate: float = 0.55,
    crop_box: tuple[int, int, int, int] | None = None,
) -> tuple[bool, str]:
    if profile != "mobile_legends":
        return True, "skip_profile"
    if profile_looks_like_mlbb_edit(video_path, sample_frames=4):
        return False, "promo_layout"
    if crop_box is None:
        crop_box = detect_vertical_content_crop(video_path, start_sec, duration_sec)
    hud, text, cartoon = score_segment_window(
        video_path, start_sec, duration_sec, crop_box=crop_box
    )
    reject_sim = reject_example_similarity(
        video_path, start_sec, duration_sec, crop_box=crop_box
    )
    if reject_sim >= max_reject_similarity:
        return False, f"reject_example_sim={reject_sim:.2f}"
    if cartoon >= max_cartoon_ratio and hud < min_hud * 1.05:
        return False, f"non_gameplay_hud={hud:.1f}"
    if hud < min_hud:
        return False, f"low_hud={hud:.1f}"
    if text > max_text:
        return False, f"overlay_text={text:.3f}"
    frame_rate = segment_hud_frame_pass_rate(
        video_path, start_sec, duration_sec, crop_box=crop_box
    )
    if frame_rate < min_hud_frame_rate:
        return False, f"low_hud_frames={frame_rate:.2f}"
    return True, "ok"


def source_has_valid_gameplay_window(
    video_path: Path,
    *,
    profile: str = "mobile_legends",
    windows: int = 5,
    window_sec: float = 10.0,
    **segment_kwargs,
) -> tuple[bool, str]:
    """True if at least one segment window passes the montage gate."""
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        return False, "unreadable"
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    cap.release()
    duration = frame_count / fps if frame_count > 0 else 0.0
    if duration < window_sec + 1.0:
        starts = [0.0]
    else:
        margin = min(2.0, duration * 0.05)
        starts = [
            float(x)
            for x in np.linspace(margin, max(margin, duration - window_sec - margin), num=windows)
        ]
    for start in starts:
        ok, reason = segment_is_valid_for_montage(
            video_path,
            start,
            min(window_sec, max(1.0, duration - start)),
            profile=profile,
            **segment_kwargs,
        )
        if ok:
            return True, f"window@{start:.1f}s"
    return False, "no_valid_window"


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
    # Whole-file heuristic is noisy on TikTok edits (cinematic/skin ads score too high).
    score = min(1.0, hud / 32.0 * 0.55 + motion * 0.45)
    if profile_looks_like_mlbb_edit(video_path, sample_frames=min(sample_frames, 6)):
        score = min(score, 0.55)
    return score


def profile_looks_like_mlbb_edit(video_path: Path, sample_frames: int = 4) -> bool:
    """Detect skin promo / letterboxed edits (animated borders, not match HUD)."""
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        return False
    frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    if frame_count <= 0:
        cap.release()
        return False
    indices = np.linspace(0, max(frame_count - 1, 0), num=min(sample_frames, frame_count), dtype=int)
    top_bottom_edges: list[float] = []
    center_edges: list[float] = []
    for idx in indices:
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(idx))
        ok, frame = cap.read()
        if not ok:
            continue
        small = cv2.resize(frame, (320, 180))
        gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
        h, w = gray.shape
        top = gray[0 : int(h * 0.18), :]
        bottom = gray[int(h * 0.82) : h, :]
        center = gray[int(h * 0.30) : int(h * 0.70), int(w * 0.15) : int(w * 0.85)]
        for band in (top, bottom):
            edges = cv2.Canny(band, 60, 150)
            top_bottom_edges.append(float(np.count_nonzero(edges)) / max(edges.size, 1))
        edges = cv2.Canny(center, 60, 150)
        center_edges.append(float(np.count_nonzero(edges)) / max(edges.size, 1))
    cap.release()
    if not top_bottom_edges or not center_edges:
        return False
    tb = float(np.mean(top_bottom_edges))
    ce = float(np.mean(center_edges))
    return tb > 0.055 and ce < tb * 0.72


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
        if known:
            ok_window, win_reason = source_has_valid_gameplay_window(video_path)
            if not ok_window:
                return False, 0.0, f"csv_overridden:{win_reason}"
            return True, 1.0, f"csv_lookup+{win_reason}"
        return False, 0.0, "csv_lookup"

    score = heuristic_gameplay_score(video_path)
    if score >= min_score:
        ok_window, win_reason = source_has_valid_gameplay_window(video_path)
        return ok_window, score, win_reason if ok_window else "no_valid_window"
    return False, score, "heuristic"


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
