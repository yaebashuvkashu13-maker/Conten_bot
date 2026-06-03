#!/usr/bin/env python3
"""Quick gameplay vs promo/memes check for MLBB TikTok clips."""

from __future__ import annotations

import argparse
import csv
import os
import re
import subprocess
from pathlib import Path

import cv2
import numpy as np

REJECT_EXAMPLES_DIR = Path("/root/data/mlbb/reject_examples")
_REJECT_REF_HISTS: list[np.ndarray] | None = None
_REJECT_REF_MTIME: float = 0.0

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


def _band_overlay_text_score(frame: np.ndarray, y0: float, y1: float) -> float:
    small = cv2.resize(frame, (320, 180))
    gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
    h, w = gray.shape
    band = gray[int(h * y0) : int(h * y1), int(w * 0.05) : int(w * 0.95)]
    if band.size == 0:
        return 0.0
    edges = cv2.Canny(band, 70, 170)
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (max(w // 10, 11), 3))
    merged = cv2.morphologyEx(edges, cv2.MORPH_CLOSE, kernel)
    edge_ratio = float(np.count_nonzero(merged)) / float(merged.size)
    _, bright = cv2.threshold(band, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    bright_ratio = float(np.count_nonzero(bright)) / float(bright.size)
    return edge_ratio * 0.7 + min(bright_ratio, 0.35) * 0.3


def _frame_overlay_text_score(frame: np.ndarray) -> float:
    """Higher = more likely subtitles / meme text in the center of the frame."""
    return _band_overlay_text_score(frame, 0.32, 0.70)


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


def detect_horizontal_content_crop(
    video_path: Path,
    start_sec: float,
    duration_sec: float,
    *,
    sample_frames: int = 6,
    vertical_crop: tuple[int, int, int, int] | None = None,
) -> tuple[int, int, int, int] | None:
    """Crop static TikTok left/right pillarbox; return (x, y, w, h) in source pixels."""
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        return None
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
    if width < 200 or height < 200:
        cap.release()
        return None
    y0, crop_h = 0, height
    if vertical_crop is not None:
        _vx, vy, _vw, vh = vertical_crop
        y0, crop_h = vy, vh
    end_sec = start_sec + max(duration_sec, 0.5)
    times = np.linspace(start_sec, max(start_sec + 0.1, end_sec - 0.05), num=sample_frames)
    frames: list[np.ndarray] = []
    for t in times:
        frame = _read_frame_at(cap, float(t))
        if frame is not None:
            if vertical_crop is not None:
                _vx, vy, vw, vh = vertical_crop
                frame = frame[vy : vy + vh, :]
            frames.append(frame)
    cap.release()
    if len(frames) < 2:
        return None

    small_h = min(180, max(72, crop_h))
    small_w = max(96, int(width * small_h / max(crop_h, 1)))
    col_activity = np.zeros(small_w, dtype=np.float32)
    for idx in range(1, len(frames)):
        prev = cv2.resize(frames[idx - 1], (small_w, small_h))
        curr = cv2.resize(frames[idx], (small_w, small_h))
        diff = cv2.absdiff(cv2.cvtColor(prev, cv2.COLOR_BGR2GRAY), cv2.cvtColor(curr, cv2.COLOR_BGR2GRAY))
        col_activity += diff.mean(axis=0)

    col_activity /= max(len(frames) - 1, 1)
    peak = float(np.percentile(col_activity, 90))
    if peak < 1.0:
        return None
    threshold = max(1.5, peak * 0.20)
    active = col_activity >= threshold
    if not np.any(active):
        return None
    indices = np.where(active)[0]
    left = int(indices[0])
    right = int(indices[-1])
    span = right - left + 1
    if span < int(small_w * 0.55):
        return None
    left_px = int(left * width / small_w)
    right_px = int((right + 1) * width / small_w)
    crop_w = right_px - left_px
    if crop_w < int(width * 0.62):
        return None
    trim_left = left_px / width
    trim_right = (width - right_px) / width
    if trim_left < 0.07 and trim_right < 0.07:
        return None
    return left_px, y0, crop_w, crop_h


def detect_game_viewport_crop(
    video_path: Path,
    start_sec: float,
    duration_sec: float,
) -> tuple[int, int, int, int] | None:
    """Merge vertical + horizontal crop so letterbox becomes black bars in render."""
    vertical = detect_vertical_content_crop(video_path, start_sec, duration_sec)
    horizontal = detect_horizontal_content_crop(
        video_path, start_sec, duration_sec, vertical_crop=vertical
    )
    if vertical is None and horizontal is None:
        return None
    if vertical is None:
        return horizontal
    if horizontal is None:
        return vertical
    vx, vy, vw, vh = vertical
    hx, hy, hw, hh = horizontal
    x = max(vx, hx)
    y = max(vy, hy)
    w = min(vx + vw, hx + hw) - x
    h = min(vy + vh, hy + hh) - y
    if w <= 0 or h <= 0:
        return vertical
    cap = cv2.VideoCapture(str(video_path))
    full_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
    full_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
    cap.release()
    if w < int(full_w * 0.50) or h < int(full_h * 0.45):
        return vertical
    return x, y, w, h


def score_left_chat_panel(
    video_path: Path,
    start_sec: float,
    duration_sec: float,
    *,
    crop_box: tuple[int, int, int, int] | None = None,
    sample_frames: int = 5,
) -> float:
    """Higher = more likely MLBB lobby/chat (text column on the left)."""
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        return 0.0
    end_sec = start_sec + max(duration_sec, 0.5)
    times = np.linspace(start_sec, max(start_sec + 0.1, end_sec - 0.05), num=sample_frames)
    scores: list[float] = []
    for t in times:
        frame = _read_frame_at(cap, float(t))
        if frame is None:
            continue
        if crop_box is not None:
            x, y, w, h = crop_box
            frame = frame[y : y + h, x : x + w]
        small = cv2.resize(frame, (320, 180))
        gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
        h, w = gray.shape
        left_band = gray[int(h * 0.10) : int(h * 0.88), 0 : int(w * 0.38)]
        if left_band.size == 0:
            continue
        edges = cv2.Canny(left_band, 60, 150)
        edge_ratio = float(np.count_nonzero(edges)) / float(edges.size)
        _, bright = cv2.threshold(left_band, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        bright_ratio = float(np.count_nonzero(bright)) / float(bright.size)
        scores.append(edge_ratio * 0.75 + min(bright_ratio, 0.40) * 0.25)
    cap.release()
    return float(np.mean(scores)) if scores else 0.0


def _extract_audio_samples(
    video_path: Path,
    start_sec: float,
    duration_sec: float,
    *,
    sample_rate: int = 11025,
) -> np.ndarray:
    try:
        result = subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-ss",
                f"{start_sec:.3f}",
                "-t",
                f"{max(duration_sec, 0.35):.3f}",
                "-i",
                str(video_path),
                "-vn",
                "-ac",
                "1",
                "-ar",
                str(sample_rate),
                "-f",
                "s16le",
                "-",
            ],
            capture_output=True,
            check=True,
            timeout=45,
        )
        samples = np.frombuffer(result.stdout, dtype=np.int16).astype(np.float32)
        return samples / 32768.0
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        return np.array([], dtype=np.float32)


def segment_music_bed_score(
    video_path: Path,
    start_sec: float,
    duration_sec: float,
) -> float:
    """
    0..1 — higher means steady TikTok/music bed (low RMS variance).
    Combat clips have spiky gun/skill audio (higher variance).
    """
    window = min(max(duration_sec, 1.0), 9.0)
    samples = _extract_audio_samples(video_path, start_sec, window)
    if samples.size < 3000:
        return 0.0
    chunk = max(2048, samples.size // 28)
    rms_vals: list[float] = []
    for offset in range(0, len(samples) - chunk, chunk):
        block = samples[offset : offset + chunk]
        rms_vals.append(float(np.sqrt(np.mean(block * block))))
    if not rms_vals:
        return 0.0
    rms_arr = np.asarray(rms_vals, dtype=np.float32)
    mean = float(rms_arr.mean())
    if mean < 0.006:
        return 0.0
    cv = float(rms_arr.std() / mean)
    if cv >= 0.44:
        return 0.0
    return float(min(1.0, max(0.0, (0.46 - cv) * 2.0)))


def score_training_intro(
    video_path: Path,
    start_sec: float,
    *,
    crop_box: tuple[int, int, int, int] | None = None,
    probe_sec: float = 2.5,
) -> float:
    """Higher = MLBB tutorial / training intro (top banners, guide popups, weak HUD)."""
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        return 0.0
    times = np.linspace(start_sec, start_sec + max(probe_sec, 0.8), num=4)
    top_scores: list[float] = []
    center_scores: list[float] = []
    hud_strength: list[float] = []
    weak_hud = 0
    total = 0
    for t in times:
        frame = _read_frame_at(cap, float(t))
        if frame is None:
            continue
        if crop_box is not None:
            x, y, w, h = crop_box
            frame = frame[y : y + h, x : x + w]
        total += 1
        top_scores.append(_band_overlay_text_score(frame, 0.0, 0.28))
        center_scores.append(_band_overlay_text_score(frame, 0.30, 0.74))
        mini, skill, _top = _frame_hud_metrics(frame)
        hud_strength.append(mini + skill)
        if mini < 7.8 or skill < 6.6:
            weak_hud += 1
    cap.release()
    if not top_scores:
        return 0.0
    top_mean = float(np.mean(top_scores))
    center_mean = float(np.mean(center_scores))
    hud_mean = float(np.mean(hud_strength))
    # Real matches keep a strong minimap + skill bar even with TikTok headers.
    if hud_mean >= 15.0:
        return 0.0
    if top_mean < 0.10:
        return 0.0
    hud_ratio = weak_hud / max(total, 1)
    if hud_ratio < 0.5:
        return 0.0
    return top_mean * 0.50 + center_mean * 0.22 + hud_ratio * 0.28


def segment_opens_with_training(
    video_path: Path,
    start_sec: float,
    *,
    crop_box: tuple[int, int, int, int] | None = None,
) -> bool:
    threshold = float(os.environ.get("SMART_MAX_TRAINING_INTRO", "0.14"))
    return score_training_intro(video_path, start_sec, crop_box=crop_box) >= threshold


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


def _reject_examples_mtime() -> float:
    if not REJECT_EXAMPLES_DIR.exists():
        return 0.0
    latest = 0.0
    for path in REJECT_EXAMPLES_DIR.glob("reject_*.*"):
        if path.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}:
            latest = max(latest, path.stat().st_mtime)
    return latest


def _reject_reference_histograms() -> list[np.ndarray]:
    global _REJECT_REF_HISTS, _REJECT_REF_MTIME
    mtime = _reject_examples_mtime()
    if _REJECT_REF_HISTS is not None and mtime == _REJECT_REF_MTIME:
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
    _REJECT_REF_MTIME = mtime
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


def score_segment_combat(
    video_path: Path,
    start_sec: float,
    duration_sec: float,
    *,
    crop_box: tuple[int, int, int, int] | None = None,
    sample_frames: int = 6,
) -> tuple[float, float, float, float]:
    """
    Returns (center_motion, minimap_delta, skill_delta, center_text).
    Real teamfight clips have motion in the arena + minimap/skill bar changes.
    """
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        return 0.0, 0.0, 0.0, 1.0
    end_sec = start_sec + max(duration_sec, 0.5)
    times = np.linspace(start_sec, max(start_sec + 0.1, end_sec - 0.05), num=sample_frames)
    center_motions: list[float] = []
    mini_deltas: list[float] = []
    skill_deltas: list[float] = []
    center_texts: list[float] = []
    prev_center: np.ndarray | None = None
    prev_mini: np.ndarray | None = None
    prev_skill: np.ndarray | None = None

    for t in times:
        frame = _read_frame_at(cap, float(t))
        if frame is None:
            continue
        if crop_box is not None:
            x, y, w, h = crop_box
            frame = frame[y : y + h, x : x + w]
        small = cv2.resize(frame, (320, 180))
        gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
        h, w = gray.shape
        center = gray[int(h * 0.22) : int(h * 0.68), int(w * 0.12) : int(w * 0.88)]
        mini = gray[int(h * 0.72) : h, 0 : int(w * 0.28)]
        skill = gray[int(h * 0.72) : h, int(w * 0.55) : w]
        center_texts.append(_band_overlay_text_score(frame, 0.25, 0.72))
        if prev_center is not None and center.size and prev_center.shape == center.shape:
            center_motions.append(float(cv2.absdiff(center, prev_center).mean()) / 255.0)
        if prev_mini is not None and mini.size and prev_mini.shape == mini.shape:
            mini_deltas.append(float(cv2.absdiff(mini, prev_mini).mean()) / 255.0)
        if prev_skill is not None and skill.size and prev_skill.shape == skill.shape:
            skill_deltas.append(float(cv2.absdiff(skill, prev_skill).mean()) / 255.0)
        prev_center, prev_mini, prev_skill = center, mini, skill

    cap.release()
    if not center_motions:
        return 0.0, 0.0, 0.0, float(np.mean(center_texts) if center_texts else 1.0)
    return (
        float(np.mean(center_motions)),
        float(np.mean(mini_deltas)) if mini_deltas else 0.0,
        float(np.mean(skill_deltas)) if skill_deltas else 0.0,
        float(np.mean(center_texts)) if center_texts else 0.0,
    )


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
    if crop_box is None:
        crop_box = detect_game_viewport_crop(video_path, start_sec, duration_sec)
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

    min_center_motion = float(os.environ.get("SMART_MIN_CENTER_MOTION", "0.016"))
    min_minimap_delta = float(os.environ.get("SMART_MIN_MINIMAP_DELTA", "0.009"))
    max_center_text = float(os.environ.get("SMART_MAX_CENTER_TEXT", "0.11"))

    center_motion, mini_delta, skill_delta, center_text = score_segment_combat(
        video_path, start_sec, duration_sec, crop_box=crop_box
    )
    max_chat_panel = float(os.environ.get("SMART_MAX_CHAT_PANEL", "0.17"))
    chat_panel = score_left_chat_panel(
        video_path, start_sec, duration_sec, crop_box=crop_box
    )
    if chat_panel > max_chat_panel and center_motion < min_center_motion * 1.45:
        return False, f"chat_lobby=panel{chat_panel:.2f}"
    if os.environ.get("SMART_REJECT_TRAINING", "1") == "1" and segment_opens_with_training(
        video_path, start_sec, crop_box=crop_box
    ):
        return False, "training_intro"
    if center_text > max_center_text and center_motion < min_center_motion * 1.6:
        return False, f"tutorial_ui=text={center_text:.2f}"
    if os.environ.get("SMART_REJECT_MUSIC_BED", "1") == "1":
        bed = segment_music_bed_score(video_path, start_sec, duration_sec)
        max_bed = float(os.environ.get("SMART_MAX_MUSIC_BED", "0.50"))
        if bed >= max_bed:
            return False, f"music_bed={bed:.2f}"
    if center_motion < min_center_motion:
        return False, f"no_combat_motion={center_motion:.3f}"
    if mini_delta < min_minimap_delta and center_motion < min_center_motion * 1.35:
        return False, f"static_scene=mini{mini_delta:.3f}"
    if skill_delta < 0.006 and center_motion < min_center_motion * 1.2 and mini_delta < min_minimap_delta * 1.5:
        return False, f"no_fight_activity=skill{skill_delta:.3f}"

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
    """Detect skin promo / stacked TikTok templates (header+footer, not match HUD)."""
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
    top_text: list[float] = []
    bottom_text: list[float] = []
    for idx in indices:
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(idx))
        ok, frame = cap.read()
        if not ok:
            continue
        top_text.append(_band_overlay_text_score(frame, 0.0, 0.22))
        bottom_text.append(_band_overlay_text_score(frame, 0.76, 1.0))
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
    tt = float(np.mean(top_text)) if top_text else 0.0
    bt = float(np.mean(bottom_text)) if bottom_text else 0.0
    # Template like "MIYA ... GAMEPLAY" with @handle header.
    if tt >= 0.10 and bt >= 0.14:
        return True
    if bt >= 0.20 and ce < bt * 1.1:
        return True
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
            if ok_window:
                return True, 1.0, f"csv_lookup+{win_reason}"
            # Stale CSV tag — re-check with heuristics instead of blocking montage.
            score = heuristic_gameplay_score(video_path)
            if score >= min_score:
                ok_retry, win_reason = source_has_valid_gameplay_window(video_path)
                if ok_retry:
                    return True, score, f"csv_stale+{win_reason}"
            return False, score, f"csv_no_window:{win_reason}"
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
