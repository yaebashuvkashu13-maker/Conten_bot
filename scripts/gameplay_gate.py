#!/usr/bin/env python3
"""Quick gameplay vs promo/memes check for MLBB TikTok clips."""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import subprocess
from pathlib import Path

import cv2
import numpy as np

try:
    from video_frame_io import prefer_ffmpeg_decode, video_pixel_size
except ImportError:
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from video_frame_io import prefer_ffmpeg_decode, video_pixel_size

REJECT_EXAMPLES_DIR = Path("/root/data/mlbb/reject_examples")
CALIBRATION_LABELS_PATH = Path("/root/data/mlbb/calibration_labels.json")
_CALIBRATION_CACHE: dict | None = None
_CALIBRATION_MTIME: float = 0.0
_REJECT_REF_HISTS: list[np.ndarray] | None = None
_REJECT_REF_MTIME: float = 0.0

PROMO_PATTERNS = re.compile(
    r"(#ad\b|sponsored|giveaway|promo\b|free\s+diamond|skin\s+gratis|"
    r"log\s*in\s+mlbb|mailbox|click\s+link|download\s+now|official\s+event)",
    re.I,
)


def _load_calibration_state() -> dict:
    global _CALIBRATION_CACHE, _CALIBRATION_MTIME
    mtime = CALIBRATION_LABELS_PATH.stat().st_mtime if CALIBRATION_LABELS_PATH.exists() else 0.0
    if _CALIBRATION_CACHE is not None and mtime == _CALIBRATION_MTIME:
        return _CALIBRATION_CACHE
    state: dict = {"good_stems": set(), "bad_stems": set(), "bad_paths": set()}
    if CALIBRATION_LABELS_PATH.exists():
        try:
            data = json.loads(CALIBRATION_LABELS_PATH.read_text(encoding="utf-8"))
            for row in data.get("good", []):
                state["good_stems"].add(Path(row.get("path", "")).stem)
            for row in data.get("bad", []):
                p = Path(row.get("path", ""))
                state["bad_stems"].add(p.stem)
                state["bad_paths"].add(str(p))
        except (json.JSONDecodeError, OSError):
            pass
    _CALIBRATION_CACHE = state
    _CALIBRATION_MTIME = mtime
    return state


def path_blocked_by_calibration(video_path: Path) -> bool:
    state = _load_calibration_state()
    stem = video_path.stem
    resolved = str(video_path.resolve())
    return stem in state["bad_stems"] or resolved in state["bad_paths"]


def path_whitelisted_by_calibration(video_path: Path) -> bool:
    state = _load_calibration_state()
    return video_path.stem in state["good_stems"]


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


PROFILE = "mobile_legends"


def _analysis_resize(frame: np.ndarray) -> np.ndarray:
    try:
        from video_orientation import resize_for_analysis

        return resize_for_analysis(frame)
    except ImportError:
        return cv2.resize(frame, (320, 180))


def extract_video_id(path: Path, description: str = "") -> str | None:
    text = f"{path.name} {description} {path}"
    match = re.search(r"(\d{10,22})", text)
    return match.group(1) if match else None


def _frame_hud_metrics(frame: np.ndarray) -> tuple[float, float, float]:
    try:
        from video_orientation import resize_for_analysis
    except ImportError:
        resize_for_analysis = lambda f: cv2.resize(f, (320, 180))  # type: ignore

    small = resize_for_analysis(frame)
    gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
    h, w = gray.shape
    minimap = gray[int(h * 0.72) : h, 0 : int(w * 0.28)]
    skill_bar = gray[int(h * 0.72) : h, int(w * 0.55) : w]
    top_hud = gray[0 : int(h * 0.16), :]
    return float(np.std(minimap)), float(np.std(skill_bar)), float(np.std(top_hud))


def _band_overlay_text_score(frame: np.ndarray, y0: float, y1: float) -> float:
    small = _analysis_resize(frame)
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


def _read_frame_at(
    video_path: Path,
    t_sec: float,
    cap: cv2.VideoCapture | None = None,
) -> np.ndarray | None:
    try:
        from video_frame_io import read_frame_at
    except ImportError:
        import sys
        from pathlib import Path as _Path

        sys.path.insert(0, str(_Path(__file__).resolve().parent))
        from video_frame_io import read_frame_at
    return read_frame_at(video_path, t_sec, cap)


def detect_vertical_content_crop(
    video_path: Path,
    start_sec: float,
    duration_sec: float,
    *,
    sample_frames: int = 6,
) -> tuple[int, int, int, int] | None:
    """Crop static TikTok header/footer bands; return (x, y, w, h) in source pixels."""
    try:
        from video_frame_io import prefer_ffmpeg_decode, video_pixel_size
    except ImportError:
        prefer_ffmpeg_decode = lambda _p: False  # type: ignore
        video_pixel_size = lambda _p: (0, 0)  # type: ignore

    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened() and not prefer_ffmpeg_decode(video_path):
        return None
    width, height = video_pixel_size(video_path)
    if width < 120 or height < 200:
        width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0) if cap.isOpened() else width
        height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0) if cap.isOpened() else height
    if width < 120 or height < 200:
        cap.release()
        return None
    end_sec = start_sec + max(duration_sec, 0.5)
    times = np.linspace(start_sec, max(start_sec + 0.1, end_sec - 0.05), num=sample_frames)
    frames: list[np.ndarray] = []
    for t in times:
        frame = _read_frame_at(video_path, float(t), cap if cap.isOpened() else None)
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
    if not cap.isOpened() and not prefer_ffmpeg_decode(video_path):
        return None
    width, height = video_pixel_size(video_path)
    if width < 200 or height < 200:
        width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0) if cap.isOpened() else width
        height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0) if cap.isOpened() else height
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
        frame = _read_frame_at(video_path, float(t), cap)
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
    min_span = float(os.environ.get("SMART_CROP_MIN_H_SPAN", "0.50"))
    if span < int(small_w * min_span):
        return None
    left_px = int(left * width / small_w)
    right_px = int((right + 1) * width / small_w)
    crop_w = right_px - left_px
    min_w_ratio = float(os.environ.get("SMART_CROP_MIN_W_RATIO", "0.58"))
    if crop_w < int(width * min_w_ratio):
        return None
    trim_left = left_px / width
    trim_right = (width - right_px) / width
    min_side_trim = float(os.environ.get("SMART_CROP_MIN_SIDE_TRIM", "0.04"))
    if trim_left < min_side_trim and trim_right < min_side_trim:
        return None
    return left_px, y0, crop_w, crop_h


def _corner_webcam_score(frame: np.ndarray, corner: str) -> float:
    """Higher = more likely a persistent face-cam PIP in a corner."""
    small = _analysis_resize(frame)
    h, w = small.shape[:2]
    boxes = {
        "bl": (0, int(h * 0.62), int(w * 0.28), h - int(h * 0.62)),
        "br": (int(w * 0.72), int(h * 0.62), w - int(w * 0.72), h - int(h * 0.62)),
        "tl": (0, 0, int(w * 0.26), int(h * 0.30)),
    }
    x0, y0, cw, ch = boxes.get(corner, boxes["bl"])
    if cw <= 0 or ch <= 0:
        return 0.0
    patch = small[y0 : y0 + ch, x0 : x0 + cw]
    gray = cv2.cvtColor(patch, cv2.COLOR_BGR2GRAY)
    edges = cv2.Canny(gray, 45, 130)
    edge_ratio = float(np.count_nonzero(edges)) / max(edges.size, 1)
    hsv = cv2.cvtColor(patch, cv2.COLOR_BGR2HSV)
    sat = hsv[:, :, 1].astype(np.float32) / 255.0
    val = hsv[:, :, 2].astype(np.float32) / 255.0
    skin = ((hsv[:, :, 0] >= 0) & (hsv[:, :, 0] <= 35) & (sat >= 0.12) & (val >= 0.18))
    skin_ratio = float(np.count_nonzero(skin)) / max(skin.size, 1)
    return edge_ratio * 0.55 + min(skin_ratio, 0.35) * 0.45


def detect_webcam_pip_crop(
    video_path: Path,
    start_sec: float,
    duration_sec: float,
    *,
    base_crop: tuple[int, int, int, int] | None = None,
    sample_frames: int = 5,
) -> tuple[int, int, int, int] | None:
    """Crop out streamer webcam PIP (bottom-left / bottom-right / top-left)."""
    if os.environ.get("SMART_CROP_WEBCAM", "1") != "1":
        return base_crop
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened() and not prefer_ffmpeg_decode(video_path):
        return base_crop
    full_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
    full_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
    end_sec = start_sec + max(duration_sec, 0.5)
    times = np.linspace(start_sec, max(start_sec + 0.1, end_sec - 0.05), num=sample_frames)
    corner_hits = {"bl": 0, "br": 0, "tl": 0}
    total = 0
    for t in times:
        frame = _read_frame_at(video_path, float(t), cap)
        if frame is None:
            continue
        total += 1
        for corner in corner_hits:
            if _corner_webcam_score(frame, corner) >= 0.085:
                corner_hits[corner] += 1
    cap.release()
    if total < 2:
        return base_crop

    pick = max(corner_hits, key=corner_hits.get)
    if corner_hits[pick] < max(2, (total + 1) // 2):
        return base_crop

    bx, by, bw, bh = base_crop or (0, 0, full_w, full_h)
    if pick == "bl":
        nx = min(full_w - 80, bx + int(bw * 0.24))
        ny = by
        nw = max(80, bx + bw - nx)
        nh = max(80, int(bh * 0.88))
        return nx, ny, nw, nh
    if pick == "br":
        nw = max(80, int(bw * 0.76))
        return bx, by, nw, bh
    # top-left webcam: trim top + left
    nx = min(full_w - 80, bx + int(bw * 0.22))
    ny = min(full_h - 80, by + int(bh * 0.18))
    nw = max(80, bx + bw - nx)
    nh = max(80, by + bh - ny)
    return nx, ny, nw, nh


def detect_game_viewport_crop(
    video_path: Path,
    start_sec: float,
    duration_sec: float,
) -> tuple[int, int, int, int] | None:
    """Merge vertical + horizontal crop; strip letterbox and webcam PIP."""
    vertical = detect_vertical_content_crop(video_path, start_sec, duration_sec)
    horizontal = detect_horizontal_content_crop(
        video_path, start_sec, duration_sec, vertical_crop=vertical
    )
    if vertical is None and horizontal is None:
        merged = None
    elif vertical is None:
        merged = horizontal
    elif horizontal is None:
        merged = vertical
    else:
        vx, vy, vw, vh = vertical
        hx, hy, hw, hh = horizontal
        x = max(vx, hx)
        y = max(vy, hy)
        w = min(vx + vw, hx + hw) - x
        h = min(vy + vh, hy + hh) - y
        if w <= 0 or h <= 0:
            merged = vertical
        else:
            cap = cv2.VideoCapture(str(video_path))
            full_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
            full_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
            cap.release()
            merged = vertical if w < int(full_w * 0.50) or h < int(full_h * 0.45) else (x, y, w, h)

    return detect_webcam_pip_crop(
        video_path, start_sec, duration_sec, base_crop=merged
    )


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
    if not cap.isOpened() and not prefer_ffmpeg_decode(video_path):
        return 0.0
    end_sec = start_sec + max(duration_sec, 0.5)
    times = np.linspace(start_sec, max(start_sec + 0.1, end_sec - 0.05), num=sample_frames)
    scores: list[float] = []
    for t in times:
        frame = _read_frame_at(video_path, float(t), cap)
        if frame is None:
            continue
        if crop_box is not None:
            x, y, w, h = crop_box
            frame = frame[y : y + h, x : x + w]
        small = _analysis_resize(frame)
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
    if not cap.isOpened() and not prefer_ffmpeg_decode(video_path):
        return 0.0
    times = np.linspace(start_sec, start_sec + max(probe_sec, 0.8), num=4)
    top_scores: list[float] = []
    center_scores: list[float] = []
    hud_strength: list[float] = []
    weak_hud = 0
    total = 0
    for t in times:
        frame = _read_frame_at(video_path, float(t), cap)
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
    if not cap.isOpened() and not prefer_ffmpeg_decode(video_path):
        return 0.0, 1.0, 1.0
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    end_sec = start_sec + max(duration_sec, 0.5)
    times = np.linspace(start_sec, max(start_sec + 0.1, end_sec - 0.05), num=sample_frames)
    hud_vals: list[float] = []
    text_vals: list[float] = []
    low_hud_frames = 0
    for t in times:
        frame = _read_frame_at(video_path, float(t), cap)
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
    small = _analysis_resize(frame)
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
    if not cap.isOpened() and not prefer_ffmpeg_decode(video_path):
        return 0.0
    end_sec = start_sec + max(duration_sec, 0.5)
    times = np.linspace(start_sec, max(start_sec + 0.1, end_sec - 0.05), num=3)
    best = 0.0
    for t in times:
        frame = _read_frame_at(video_path, float(t), cap)
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
    if not cap.isOpened() and not prefer_ffmpeg_decode(video_path):
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
        frame = _read_frame_at(video_path, float(t), cap)
        if frame is None:
            continue
        if crop_box is not None:
            x, y, w, h = crop_box
            frame = frame[y : y + h, x : x + w]
        small = _analysis_resize(frame)
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


def segment_minimap_presence_rate(
    video_path: Path,
    start_sec: float,
    duration_sec: float,
    *,
    crop_box: tuple[int, int, int, int] | None = None,
    sample_frames: int = 5,
    min_minimap: float = 7.5,
    min_skill: float = 6.5,
) -> float:
    """Share of frames with visible minimap + skill bar (real in-match HUD)."""
    return segment_hud_frame_pass_rate(
        video_path,
        start_sec,
        duration_sec,
        crop_box=crop_box,
        sample_frames=sample_frames,
        min_minimap=min_minimap,
        min_skill=min_skill,
    )


def segment_looks_like_draft_or_queue(
    video_path: Path,
    start_sec: float,
    duration_sec: float,
    *,
    crop_box: tuple[int, int, int, int] | None = None,
) -> bool:
    """Hero pick, loading, queue wait — not active teamfight."""
    if segment_looks_like_hero_showcase(
        video_path, start_sec, duration_sec, crop_box=crop_box, sample_frames=5
    ):
        return True
    center_motion, mini_delta, skill_delta, center_text = score_segment_combat(
        video_path, start_sec, duration_sec, crop_box=crop_box, sample_frames=6
    )
    hud_rate = segment_minimap_presence_rate(
        video_path, start_sec, duration_sec, crop_box=crop_box, sample_frames=5
    )
    if hud_rate < 0.34 and center_motion < 0.014:
        return True
    if hud_rate < 0.50 and center_motion < 0.018 and mini_delta < 0.006:
        return True
    if segment_opens_with_training(video_path, start_sec, crop_box=crop_box):
        return True
    if center_text > 0.14 and center_motion < 0.020 and skill_delta < 0.007:
        return True
    chat_panel = score_left_chat_panel(
        video_path, start_sec, duration_sec, crop_box=crop_box, sample_frames=4
    )
    if chat_panel > 0.14 and hud_rate < 0.55 and center_motion < 0.022:
        return True
    return False


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
    if not cap.isOpened() and not prefer_ffmpeg_decode(video_path):
        return 0.0
    end_sec = start_sec + max(duration_sec, 0.5)
    times = np.linspace(start_sec, max(start_sec + 0.1, end_sec - 0.05), num=sample_frames)
    passed = 0
    total = 0
    for t in times:
        frame = _read_frame_at(video_path, float(t), cap)
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


def segment_looks_like_interview_or_talk(
    video_path: Path,
    start_sec: float,
    duration_sec: float,
    *,
    crop_box: tuple[int, int, int, int] | None = None,
) -> bool:
    """Podcast / face-cam / interview — weak HUD, heavy top or center captions."""
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened() and not prefer_ffmpeg_decode(video_path):
        return False
    end_sec = start_sec + max(duration_sec, 0.5)
    times = np.linspace(start_sec, max(start_sec + 0.1, end_sec - 0.05), num=4)
    weak_hud = 0
    top_heavy = 0
    center_heavy = 0
    for t in times:
        frame = _read_frame_at(video_path, float(t), cap)
        if frame is None:
            continue
        if crop_box is not None:
            x, y, w, h = crop_box
            frame = frame[y : y + h, x : x + w]
        mini, skill, _top = _frame_hud_metrics(frame)
        if mini + skill < 14.0:
            weak_hud += 1
        if _band_overlay_text_score(frame, 0.0, 0.26) >= 0.12:
            top_heavy += 1
        if _band_overlay_text_score(frame, 0.30, 0.78) >= 0.13:
            center_heavy += 1
    cap.release()
    if weak_hud >= 3 and (top_heavy >= 2 or center_heavy >= 3):
        return True
    return False


def segment_looks_like_meme_comic(
    video_path: Path,
    start_sec: float,
    duration_sec: float,
    *,
    crop_box: tuple[int, int, int, int] | None = None,
) -> bool:
    """Meme / comic strips in the center — dense text, little real match HUD motion."""
    center_motion, mini_delta, _skill, center_text = score_segment_combat(
        video_path, start_sec, duration_sec, crop_box=crop_box, sample_frames=5
    )
    if center_text >= 0.13 and center_motion < 0.022:
        return True
    if center_text >= 0.11 and mini_delta < 0.007 and center_motion < 0.028:
        return True
    return False


def _extract_segment_audio_pcm(
    video_path: Path,
    start_sec: float,
    duration_sec: float,
    *,
    sample_rate: int = 11025,
) -> np.ndarray:
    cmd = [
        "ffmpeg",
        "-v",
        "error",
        "-hwaccel",
        "none",
        "-ss",
        f"{start_sec:.3f}",
        "-t",
        f"{duration_sec:.3f}",
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
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, check=False, timeout=45)
    except subprocess.TimeoutExpired:
        return np.array([], dtype=np.int16)
    if result.returncode != 0 or not result.stdout:
        return np.array([], dtype=np.int16)
    return np.frombuffer(result.stdout, dtype=np.int16)


def score_pubg_gunfire_audio(
    video_path: Path,
    start_sec: float,
    duration_sec: float,
) -> tuple[float, float, float]:
    """Gunshot-like transients in segment audio: density, peak/rms, mean rms."""
    samples = _extract_segment_audio_pcm(video_path, start_sec, duration_sec)
    if samples.size < 384:
        return 0.0, 0.0, 0.0
    pcm = samples.astype(np.float32) / 32768.0
    frame = 256
    energies: list[float] = []
    for offset in range(0, len(pcm) - frame, frame):
        chunk = pcm[offset : offset + frame]
        energies.append(float(np.sqrt(np.mean(chunk * chunk))))
    if len(energies) < 3:
        return 0.0, 0.0, 0.0
    arr = np.asarray(energies, dtype=np.float32)
    median = float(np.median(arr))
    peak = float(np.max(arr))
    rms = float(np.mean(arr))
    floor = max(median * 2.6, 0.010)
    spikes = 0
    for idx in range(1, len(arr)):
        if arr[idx] > floor and arr[idx] > arr[idx - 1] * 1.55:
            spikes += 1
    density = spikes / max(len(arr) - 1, 1)
    burst_ratio = peak / max(rms, 1e-6)
    return density, burst_ratio, rms


def segment_looks_like_pubg_loot_or_walk(
    video_path: Path,
    start_sec: float,
    duration_sec: float,
    *,
    crop_box: tuple[int, int, int, int] | None = None,
    gunfire_density: float,
) -> bool:
    """Running/looting/inventory — motion without gunfire transients."""
    center_motion, _mini_delta, _skill_delta, center_text = score_segment_combat(
        video_path, start_sec, duration_sec, crop_box=crop_box, sample_frames=5
    )
    min_gun = float(os.environ.get("SMART_PUBG_MIN_GUNFIRE_DENSITY", "0.055"))
    if gunfire_density >= min_gun:
        return False
    if center_motion >= 0.028 and gunfire_density < min_gun * 0.75:
        return True
    if center_motion < 0.014 and gunfire_density < min_gun * 0.55:
        return True
    if center_text > 0.12 and gunfire_density < min_gun * 0.8:
        return True
    return False


def _genshin_boss_bar_score(frame: np.ndarray) -> float:
    """Boss HP bar at top center — red/orange horizontal strip (0..1)."""
    small = _analysis_resize(frame)
    top = small[0 : int(180 * 0.13), int(320 * 0.12) : int(320 * 0.88)]
    if top.size == 0:
        return 0.0
    hsv = cv2.cvtColor(top, cv2.COLOR_BGR2HSV)
    red_a = cv2.inRange(hsv, (0, 70, 70), (12, 255, 255))
    red_b = cv2.inRange(hsv, (168, 70, 70), (180, 255, 255))
    orange = cv2.inRange(hsv, (8, 90, 90), (32, 255, 255))
    mask = red_a | red_b | orange
    fill = float(np.count_nonzero(mask)) / float(mask.size)
    row_signal = mask.mean(axis=1) / 255.0
    strong_rows = int(np.sum(row_signal > 0.10))
    col_signal = mask.mean(axis=0) / 255.0
    wide_bar = float(np.sum(col_signal > 0.08)) / max(len(col_signal), 1)
    return min(1.0, fill * 4.2 + strong_rows * 0.06 + wide_bar * 0.35)


def score_genshin_boss_likelihood(
    video_path: Path,
    start_sec: float,
    duration_sec: float,
    *,
    crop_box: tuple[int, int, int, int] | None = None,
    sample_frames: int = 6,
) -> tuple[float, float, float, float]:
    """Returns (boss_bar, center_motion, combined boss score, bar_peak)."""
    end_sec = start_sec + max(duration_sec, 0.5)
    times = np.linspace(start_sec, max(start_sec + 0.1, end_sec - 0.05), num=sample_frames)
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened() and not prefer_ffmpeg_decode(video_path):
        return 0.0, 0.0, 0.0, 0.0
    bar_scores: list[float] = []
    center_motions: list[float] = []
    prev_center: np.ndarray | None = None
    for t in times:
        frame = _read_frame_at(video_path, float(t), cap)
        if frame is None:
            continue
        if crop_box is not None:
            x, y, w, h = crop_box
            frame = frame[y : y + h, x : x + w]
        bar_scores.append(_genshin_boss_bar_score(frame))
        small = _analysis_resize(frame)
        gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
        h, w = gray.shape
        center = gray[int(h * 0.18) : int(h * 0.72), int(w * 0.10) : int(w * 0.90)]
        if prev_center is not None and center.size and prev_center.shape == center.shape:
            center_motions.append(float(cv2.absdiff(center, prev_center).mean()) / 255.0)
        prev_center = center
    cap.release()
    boss_bar = float(np.mean(bar_scores)) if bar_scores else 0.0
    center_motion = float(np.mean(center_motions)) if center_motions else 0.0
    bar_peak = float(np.max(bar_scores)) if bar_scores else 0.0
    combined = min(
        1.0,
        boss_bar * 0.70
        + bar_peak * 0.22
        + min(center_motion * 2.5, 0.10)
        + (
            0.06
            if boss_bar > 0.18 and bar_peak > 0.25 and center_motion > 0.022
            else 0.0
        ),
    )
    return boss_bar, center_motion, combined, bar_peak


def segment_is_valid_for_montage(
    video_path: Path,
    start_sec: float,
    duration_sec: float,
    *,
    profile: str = "mobile_legends",
    min_hud: float = 14.0,
    max_text: float = 0.075,
    max_cartoon_ratio: float = 0.50,
    max_reject_similarity: float = 0.72,
    min_hud_frame_rate: float = 0.55,
    min_gunfire: float | None = None,
    min_boss: float | None = None,
    crop_box: tuple[int, int, int, int] | None = None,
) -> tuple[bool, str]:
    if profile == "genshin" and os.environ.get("SMART_GENSHIN_REQUIRE_BOSS", "1") == "1":
        if crop_box is None:
            crop_box = detect_game_viewport_crop(video_path, start_sec, duration_sec)
        boss_bar, center_motion, boss_score, bar_peak = score_genshin_boss_likelihood(
            video_path, start_sec, duration_sec, crop_box=crop_box
        )
        min_bar = (
            float(os.environ.get("SMART_GENSHIN_MIN_BOSS_BAR", "0.20"))
            if min_boss is None
            else min_boss
        )
        min_peak = float(os.environ.get("SMART_GENSHIN_MIN_BOSS_BAR_PEAK", "0.28"))
        min_motion = float(os.environ.get("SMART_GENSHIN_MIN_CENTER_MOTION", "0.020"))
        min_audio = float(os.environ.get("SMART_GENSHIN_MIN_AUDIO_RMS", "0.012"))
        if boss_bar < min_bar and bar_peak < min_peak:
            return False, f"no_boss_bar=bar{boss_bar:.3f}:peak{bar_peak:.3f}"
        if boss_bar < min_bar * 0.90 and bar_peak < min_peak * 0.92:
            return False, f"mob_not_boss=bar{boss_bar:.3f}:peak{bar_peak:.3f}"
        _impact_density, _burst_ratio, audio_rms = score_pubg_gunfire_audio(
            video_path, start_sec, duration_sec
        )
        if center_motion < min_motion and audio_rms < min_audio:
            return False, f"idle=motion{center_motion:.3f}:rms{audio_rms:.4f}"
        if (
            boss_bar >= min_bar
            and center_motion < min_motion * 0.80
            and audio_rms < min_audio * 1.15
        ):
            return False, f"false_boss_ui=motion{center_motion:.3f}:rms{audio_rms:.4f}"
        min_score = float(os.environ.get("SMART_GENSHIN_MIN_BOSS_SCORE", "0.32"))
        if boss_score < min_score and bar_peak < min_peak * 1.05:
            return False, f"weak_boss=score{boss_score:.2f}:peak{bar_peak:.3f}"
        return True, "boss_ok"
    if profile in ("wot", "world_of_tanks"):
        if crop_box is None:
            crop_box = detect_game_viewport_crop(video_path, start_sec, duration_sec)
        impact_density, burst_ratio, audio_rms = score_pubg_gunfire_audio(
            video_path, start_sec, duration_sec
        )
        min_impact = (
            float(os.environ.get("SMART_WOT_MIN_IMPACT_DENSITY", "0.052"))
            if min_gunfire is None
            else min_gunfire
        )
        min_burst = float(os.environ.get("SMART_WOT_MIN_BURST_RATIO", "2.3"))
        min_audio = float(os.environ.get("SMART_WOT_MIN_AUDIO_RMS", "0.010"))
        if impact_density < min_impact and burst_ratio < min_burst:
            return False, f"no_hits=density{impact_density:.3f}:burst{burst_ratio:.2f}"
        if audio_rms < min_audio and impact_density < min_impact * 1.15:
            return False, f"silent_drive=rms{audio_rms:.4f}"
        center_motion, _mini_delta, _skill_delta, _center_text = score_segment_combat(
            video_path, start_sec, duration_sec, crop_box=crop_box, sample_frames=5
        )
        if center_motion < float(os.environ.get("SMART_WOT_MIN_CENTER_MOTION", "0.014")):
            if impact_density < min_impact * 1.1:
                return False, f"cruise_no_action=motion{center_motion:.3f}"
        if impact_density < min_impact * 0.85 and center_motion < 0.020:
            return False, f"empty_drive=density{impact_density:.3f}"
        if center_motion >= 0.10 and impact_density < max(min_impact * 1.35, 0.070):
            return False, f"cruise_no_action=motion{center_motion:.3f}:impact{impact_density:.3f}"
        return True, "brawl_ok"
    if profile in ("pubg", "standoff"):
        prefix = "SMART_PUBG_" if profile == "pubg" else "SMART_STANDOFF_"
        if crop_box is None:
            crop_box = detect_game_viewport_crop(video_path, start_sec, duration_sec)
        gunfire_density, burst_ratio, audio_rms = score_pubg_gunfire_audio(
            video_path, start_sec, duration_sec
        )
        center_motion, _mini_delta, _skill_delta, center_text = score_segment_combat(
            video_path, start_sec, duration_sec, crop_box=crop_box, sample_frames=5
        )
        min_gun = (
            float(os.environ.get(f"{prefix}MIN_GUNFIRE_DENSITY", "0.055"))
            if min_gunfire is None
            else min_gunfire
        )
        min_burst = float(
            os.environ.get(
                f"{prefix}MIN_BURST_RATIO",
                "4.0" if profile == "standoff" else "2.4",
            )
        )
        min_audio = float(os.environ.get(f"{prefix}MIN_AUDIO_RMS", "0.008"))
        if profile == "pubg":
            try:
                from pubg_owner_calibration import (
                    nearest_owner_label,
                    pubg_passes_owner_heuristics,
                    segment_overlaps_owner_label,
                )
            except ImportError:
                from pubg_owner_calibration import (  # type: ignore[no-redef]
                    nearest_owner_label,
                    pubg_passes_owner_heuristics,
                    segment_overlaps_owner_label,
                )
            if segment_overlaps_owner_label(
                video_path, start_sec, duration_sec, label="bad", pad_sec=10.0
            ):
                return False, "owner_bad_window"
            ok_owner, owner_reason = pubg_passes_owner_heuristics(
                gunfire_density, burst_ratio, audio_rms, center_motion
            )
            if not ok_owner:
                return False, owner_reason
            if owner_reason == "sniper_hold" and center_motion < 0.030:
                return False, f"sniper_hold_no_motion=motion{center_motion:.3f}"
            if os.environ.get("SMART_PUBG_TIKTOK_COMBAT", "0") == "1":
                try:
                    from pubg_owner_calibration import pubg_passes_tiktok_combat_gate
                except ImportError:
                    from pubg_owner_calibration import pubg_passes_tiktok_combat_gate  # type: ignore
                ok_tt, tt_reason = pubg_passes_tiktok_combat_gate(
                    video_path,
                    start_sec,
                    gunfire_density,
                    burst_ratio,
                    center_motion=center_motion,
                )
                if not ok_tt:
                    return False, tt_reason
            strict_gun = gunfire_density >= min_gun and burst_ratio >= min_burst
            heuristic_gun = owner_reason in ("fight_audio", "light_combat")
            if not strict_gun and not heuristic_gun:
                if owner_reason == "sniper_hold" and center_motion >= 0.030:
                    pass
                else:
                    return False, f"no_shots=density{gunfire_density:.3f}:burst{burst_ratio:.2f}"
            talk_rms = float(os.environ.get("SMART_PUBG_MAX_TALK_RMS", "0.034"))
            if audio_rms > talk_rms and gunfire_density < min_gun * 1.08:
                return False, f"streamer_talk=rms{audio_rms:.4f}:gun{gunfire_density:.3f}"
        elif (
            gunfire_density < min_gun
            and burst_ratio < min_burst
            and audio_rms < min_audio * 1.10
        ):
            return False, f"low_gunfire=density{gunfire_density:.3f}:burst{burst_ratio:.2f}"
        if audio_rms < min_audio * 0.85 and gunfire_density < min_gun * 0.90:
            return False, f"silent_segment=rms{audio_rms:.4f}"
        min_center_motion = float(os.environ.get(f"{prefix}MIN_CENTER_MOTION", "0.018"))
        if profile == "pubg":
            sniper_ok, _ = pubg_passes_owner_heuristics(
                gunfire_density, burst_ratio, audio_rms, center_motion
            )
            if sniper_ok and center_motion < min_center_motion:
                min_center_motion = 0.010
        if center_motion < min_center_motion:
            return False, f"no_aim_motion={center_motion:.3f}"
        default_max_text = "0.62" if profile == "pubg" else "0.14"
        max_text = float(os.environ.get(f"{prefix}MAX_CENTER_TEXT", default_max_text))
        if center_text > max_text and gunfire_density < min_gun * 0.90:
            return False, f"menu_overlay={center_text:.2f}"
        if profile == "pubg":
            run_hi = float(os.environ.get("SMART_PUBG_MAX_RUN_MOTION", "0.21"))
            if (
                center_motion >= 0.09
                and center_motion <= run_hi
                and gunfire_density < min_gun * 1.20
            ):
                return False, f"run_no_fight=motion{center_motion:.3f}:gun{gunfire_density:.3f}"
        loot_cap = min_gun * (0.85 if profile == "pubg" else 1.0)
        if (
            segment_looks_like_pubg_loot_or_walk(
                video_path,
                start_sec,
                duration_sec,
                crop_box=crop_box,
                gunfire_density=gunfire_density,
            )
            and gunfire_density < loot_cap
        ):
            return False, f"loot_walk=density{gunfire_density:.3f}"
        return True, "ok"
    if profile != "mobile_legends":
        return True, "skip_profile"
    if path_blocked_by_calibration(video_path):
        return False, "calibration_bad"
    if crop_box is None:
        crop_box = detect_game_viewport_crop(video_path, start_sec, duration_sec)
    if os.environ.get("SMART_REJECT_PROMO", "1") == "1" and segment_looks_like_promo_or_cinematic(
        video_path, start_sec, duration_sec, crop_box=crop_box
    ):
        return False, "promo_layout"
    hud, text, cartoon = score_segment_window(
        video_path, start_sec, duration_sec, crop_box=crop_box
    )
    reject_sim = reject_example_similarity(
        video_path, start_sec, duration_sec, crop_box=crop_box
    )
    if reject_sim >= max_reject_similarity:
        return False, f"reject_example_sim={reject_sim:.2f}"
    if cartoon >= max_cartoon_ratio and hud < min_hud * 1.05:
        return False, f"cartoon_or_non_match={hud:.1f}"
    if cartoon >= 0.45 and hud < min_hud * 1.15:
        return False, f"cartoon_ratio={cartoon:.2f}"
    if os.environ.get("SMART_REJECT_INTERVIEW", "1") == "1" and segment_looks_like_interview_or_talk(
        video_path, start_sec, duration_sec, crop_box=crop_box
    ):
        return False, "interview_talk"
    if os.environ.get("SMART_REJECT_MEME", "1") == "1" and segment_looks_like_meme_comic(
        video_path, start_sec, duration_sec, crop_box=crop_box
    ):
        return False, "meme_comic"
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
    min_minimap_presence = float(os.environ.get("SMART_MIN_MINIMAP_PRESENCE", "0.72"))
    require_minimap = os.environ.get("SMART_REQUIRE_MINIMAP", "1") == "1"
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
    if os.environ.get("SMART_REJECT_DRAFT_QUEUE", "1") == "1" and segment_looks_like_draft_or_queue(
        video_path, start_sec, duration_sec, crop_box=crop_box
    ):
        return False, "draft_queue_loading"

    minimap_presence = segment_minimap_presence_rate(
        video_path, start_sec, duration_sec, crop_box=crop_box
    )
    if require_minimap and minimap_presence < min_minimap_presence:
        return False, f"no_minimap={minimap_presence:.2f}"

    if center_motion < min_center_motion:
        return False, f"no_combat_motion={center_motion:.3f}"
    if mini_delta < min_minimap_delta:
        return False, f"static_minimap={mini_delta:.3f}"
    if skill_delta < 0.006 and center_motion < min_center_motion * 1.2 and mini_delta < min_minimap_delta * 1.5:
        return False, f"no_fight_activity=skill{skill_delta:.3f}"

    if os.environ.get("SMART_REJECT_HERO_SHOWCASE", "1") == "1" and segment_looks_like_hero_showcase(
        video_path, start_sec, duration_sec, crop_box=crop_box
    ):
        return False, "hero_showcase"

    require_uniform = os.environ.get("SMART_REQUIRE_UNIFORM_GAMEPLAY", "1") == "1"
    if require_uniform:
        ok_uniform, uniform_reason = segment_uniform_gameplay_ok(
            video_path, start_sec, duration_sec, crop_box=crop_box, profile=profile
        )
        if not ok_uniform:
            return False, uniform_reason

    if os.environ.get("SMART_MLBB_REQUIRE_KILL_UI", "1") == "1":
        try:
            from mlbb_kill_ui import passes_mlbb_kill_gate

            ok_kill, kill_reason, _kill = passes_mlbb_kill_gate(
                video_path,
                start_sec,
                duration_sec,
                center_motion=center_motion,
                skill_delta=skill_delta,
                minimap_delta=mini_delta,
            )
            if not ok_kill:
                return False, f"no_kill_ui={kill_reason}"
        except ImportError:
            pass

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
    if not cap.isOpened() and not prefer_ffmpeg_decode(video_path):
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
    if not cap.isOpened() and not prefer_ffmpeg_decode(video_path):
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


def _frame_looks_like_promo_template(frame: np.ndarray) -> bool:
    """Single frame: TikTok skin promo / stacked template, not live match HUD."""
    top_text = _band_overlay_text_score(frame, 0.0, 0.22)
    bottom_text = _band_overlay_text_score(frame, 0.76, 1.0)
    small = _analysis_resize(frame)
    gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
    h, w = gray.shape
    top = gray[0 : int(h * 0.18), :]
    bottom = gray[int(h * 0.82) : h, :]
    center = gray[int(h * 0.30) : int(h * 0.70), int(w * 0.15) : int(w * 0.85)]
    top_bottom_edges: list[float] = []
    for band in (top, bottom):
        edges = cv2.Canny(band, 60, 150)
        top_bottom_edges.append(float(np.count_nonzero(edges)) / max(edges.size, 1))
    edges = cv2.Canny(center, 60, 150)
    center_edge = float(np.count_nonzero(edges)) / max(edges.size, 1)
    tb = float(np.mean(top_bottom_edges)) if top_bottom_edges else 0.0
    mini, skill, _top = _frame_hud_metrics(frame)
    hud_strong = mini >= 9.0 and skill >= 8.0
    hud_weak = mini < 8.0 or skill < 7.0
    # Live match with TikTok header/footer is OK; promo = weak HUD + heavy template text.
    if hud_strong and center_edge >= 0.04:
        if not (top_text >= 0.16 and bottom_text >= 0.22):
            return False
    if top_text >= 0.12 and bottom_text >= 0.16 and hud_weak:
        return True
    if bottom_text >= 0.22 and center_edge < bottom_text * 0.50 and hud_weak:
        return True
    if tb > 0.065 and center_edge < tb * 0.65 and hud_weak:
        return True
    if hud_weak and top_text >= 0.11 and bottom_text >= 0.13 and center_edge < 0.035:
        return True
    return False


def _frame_looks_like_hero_showcase(frame: np.ndarray) -> bool:
    """Skin reveal / spawn cinematic: big character art, weak or dead match HUD."""
    if _frame_looks_like_promo_template(frame):
        return False
    mini, skill, _top = _frame_hud_metrics(frame)
    if mini >= 9.2 and skill >= 8.2:
        return False
    small = _analysis_resize(frame)
    gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
    h, w = gray.shape
    center = gray[int(h * 0.14) : int(h * 0.86), int(w * 0.06) : int(w * 0.94)]
    mini_band = gray[int(h * 0.70) : h, 0 : int(w * 0.30)]
    if center.size == 0:
        return False
    center_edge = float(np.count_nonzero(cv2.Canny(center, 55, 145))) / max(center.size, 1)
    mini_edge = (
        float(np.count_nonzero(cv2.Canny(mini_band, 55, 145))) / max(mini_band.size, 1)
        if mini_band.size
        else 0.0
    )
    hud_sum = mini + skill
    if hud_sum < 13.0 and center_edge >= 0.040 and mini_edge < 0.030 and mini < 8.0:
        return True
    if hud_sum < 11.0 and center_edge >= 0.048:
        return True
    return False


def segment_looks_like_hero_showcase(
    video_path: Path,
    start_sec: float,
    duration_sec: float,
    *,
    crop_box: tuple[int, int, int, int] | None = None,
    sample_frames: int = 4,
) -> bool:
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened() and not prefer_ffmpeg_decode(video_path):
        return True
    end_sec = start_sec + max(duration_sec, 0.5)
    times = np.linspace(start_sec, max(start_sec + 0.1, end_sec - 0.05), num=sample_frames)
    showcase_hits = 0
    for t in times:
        frame = _read_frame_at(video_path, float(t), cap)
        if frame is None:
            continue
        check = frame
        if crop_box is not None:
            x, y, w, h = crop_box
            check = frame[y : y + h, x : x + w]
        if _frame_looks_like_hero_showcase(check):
            showcase_hits += 1
    cap.release()
    need = max(2, (sample_frames + 1) // 2)
    return showcase_hits >= need


def segment_uniform_gameplay_ok(
    video_path: Path,
    start_sec: float,
    duration_sec: float,
    *,
    crop_box: tuple[int, int, int, int] | None = None,
    profile: str = "mobile_legends",
) -> tuple[bool, str]:
    """
    Reject montage windows that are only half gameplay (common TikTok: fight then hero splash).
    Each temporal slice must keep HUD + combat; no promo/showcase block.
    """
    if profile != "mobile_legends" or duration_sec < 6.0:
        return True, "ok"
    parts = 3 if duration_sec >= 9.0 else 2
    part_dur = max(duration_sec / parts, 2.0)
    min_hud_rate = float(os.environ.get("SMART_UNIFORM_MIN_HUD_RATE", "0.70"))
    min_center_motion = float(os.environ.get("SMART_MIN_CENTER_MOTION", "0.016"))
    min_minimap_delta = float(os.environ.get("SMART_MIN_MINIMAP_DELTA", "0.009"))
    for idx in range(parts):
        sub_start = start_sec + idx * part_dur
        if sub_start + part_dur > start_sec + duration_sec + 0.05:
            break
        sub_dur = min(part_dur, start_sec + duration_sec - sub_start)
        if segment_looks_like_promo_or_cinematic(
            video_path, sub_start, sub_dur, crop_box=crop_box, sample_frames=5
        ):
            return False, f"promo_in_part_{idx}"
        if segment_looks_like_hero_showcase(
            video_path, sub_start, sub_dur, crop_box=crop_box, sample_frames=5
        ):
            return False, f"hero_showcase_part_{idx}"
        hud_rate = segment_hud_frame_pass_rate(
            video_path,
            sub_start,
            sub_dur,
            crop_box=crop_box,
            sample_frames=4,
            min_minimap=8.0,
            min_skill=7.0,
        )
        if hud_rate < min_hud_rate:
            return False, f"weak_hud_part_{idx}={hud_rate:.2f}"
        center_motion, mini_delta, skill_delta, _center_text = score_segment_combat(
            video_path, sub_start, sub_dur, crop_box=crop_box, sample_frames=5
        )
        if center_motion < min_center_motion * 0.88 and mini_delta < min_minimap_delta * 0.82:
            return False, f"static_part_{idx}"
        if skill_delta < 0.005 and mini_delta < min_minimap_delta and center_motion < min_center_motion:
            return False, f"no_fight_part_{idx}"
    return True, "ok"


def segment_looks_like_promo_or_cinematic(
    video_path: Path,
    start_sec: float,
    duration_sec: float,
    *,
    crop_box: tuple[int, int, int, int] | None = None,
    sample_frames: int = 4,
) -> bool:
    """True when the segment window is a promo/cinematic edit, not real gameplay."""
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened() and not prefer_ffmpeg_decode(video_path):
        return True
    end_sec = start_sec + max(duration_sec, 0.5)
    times = np.linspace(start_sec, max(start_sec + 0.1, end_sec - 0.05), num=sample_frames)
    promo_hits = 0
    showcase_hits = 0
    hud_weak = 0
    for t in times:
        frame = _read_frame_at(video_path, float(t), cap)
        if frame is None:
            continue
        check = frame
        if crop_box is not None:
            x, y, w, h = crop_box
            check = frame[y : y + h, x : x + w]
        if _frame_looks_like_promo_template(check):
            promo_hits += 1
        if _frame_looks_like_hero_showcase(check):
            showcase_hits += 1
        mini, skill, _top = _frame_hud_metrics(check)
        if mini + skill < 13.5:
            hud_weak += 1
    cap.release()
    bad_hits = promo_hits + showcase_hits
    need_bad = max(2, (sample_frames + 1) // 2)
    if bad_hits >= need_bad:
        return True
    if promo_hits >= max(2, sample_frames - 1):
        return True
    if showcase_hits >= max(2, sample_frames - 1):
        return True
    if hud_weak >= max(2, sample_frames - 1) and bad_hits >= 1:
        return True
    return False


def profile_looks_like_mlbb_edit(video_path: Path, sample_frames: int = 4) -> bool:
    """Detect skin promo / stacked TikTok templates (header+footer, not match HUD)."""
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened() and not prefer_ffmpeg_decode(video_path):
        return False
    frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    if frame_count <= 0:
        cap.release()
        return False
    indices = np.linspace(0, max(frame_count - 1, 0), num=min(sample_frames, frame_count), dtype=int)
    promo_hits = 0
    showcase_hits = 0
    for idx in indices:
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(idx))
        ok, frame = cap.read()
        if not ok:
            continue
        if _frame_looks_like_promo_template(frame):
            promo_hits += 1
        if _frame_looks_like_hero_showcase(frame):
            showcase_hits += 1
    cap.release()
    bad = promo_hits + showcase_hits
    return bad >= max(2, min(sample_frames, 3))


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
