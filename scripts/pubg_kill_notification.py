#!/usr/bin/env python3
"""Location-independent PUBG kill notification detector."""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

import numpy as np

DEFAULT_CONFIG = {
    "enabled": True,
    "sample_fps": 1.5,
    "max_frames": 18,
    "min_persistence_frames": 3,
    "min_score": 0.42,
    "min_colored_pixels": 10,
    "min_width_ratio": 0.035,
    "max_height_ratio": 0.14,
    "min_aspect_ratio": 3.0,
    "search": {"x0": 0.0, "y0": 0.0, "x1": 1.0, "y1": 0.82},
    "hsv_ranges": [
        {"name": "cyan", "low": [78, 45, 90], "high": [105, 255, 255]},
        {"name": "blue", "low": [100, 45, 70], "high": [135, 255, 255]},
    ],
}


def _repo_root() -> Path:
    return Path(os.environ.get("CONTENT_BOT_REPO", Path(__file__).resolve().parent.parent))


def config_path() -> Path:
    return Path(
        os.environ.get(
            "PUBG_KILL_NOTIFICATION_CONFIG",
            _repo_root() / "config" / "pubg_kill_notification.json",
        )
    )


def load_config() -> dict[str, Any]:
    config = json.loads(json.dumps(DEFAULT_CONFIG))
    path = config_path()
    try:
        custom = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        custom = {}
    if isinstance(custom, dict):
        config.update({key: value for key, value in custom.items() if key != "search"})
        if isinstance(custom.get("search"), dict):
            config["search"].update(custom["search"])
    return config


def _ocr_text(crop: np.ndarray) -> str:
    import cv2

    try:
        import pytesseract
    except ImportError:
        return ""
    if crop.size == 0:
        return ""
    scale = max(2.0, 720.0 / max(crop.shape[1], 1))
    enlarged = cv2.resize(crop, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
    gray = cv2.cvtColor(enlarged, cv2.COLOR_BGR2GRAY)
    variants = [
        gray,
        cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1],
    ]
    best = ""
    for image in variants:
        try:
            text = pytesseract.image_to_string(
                image,
                config=(
                    "--psm 7 "
                    "-c tessedit_char_whitelist="
                    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-+[] "
                ),
            )
        except Exception:
            continue
        cleaned = " ".join(text.split())
        if len(re.sub(r"[^A-Za-z0-9А-Яа-я]", "", cleaned)) > len(
            re.sub(r"[^A-Za-z0-9А-Яа-я]", "", best)
        ):
            best = cleaned
    return best[:120]


def _color_mask(frame: np.ndarray, config: dict[str, Any]) -> np.ndarray:
    import cv2

    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    mask = np.zeros(hsv.shape[:2], dtype=np.uint8)
    for row in config.get("hsv_ranges") or []:
        try:
            low = np.asarray(row["low"], dtype=np.uint8)
            high = np.asarray(row["high"], dtype=np.uint8)
        except (KeyError, TypeError, ValueError):
            continue
        mask = cv2.bitwise_or(mask, cv2.inRange(hsv, low, high))
    return mask


def locate_notification_regions(
    frame: np.ndarray,
    *,
    config: dict[str, Any] | None = None,
    ocr: bool = True,
) -> list[dict[str, Any]]:
    """Find colored horizontal nickname rows anywhere in the gameplay viewport."""
    import cv2

    config = config or load_config()
    if frame is None or getattr(frame, "size", 0) == 0:
        return []
    source_h, source_w = frame.shape[:2]
    scale = min(1.0, 720.0 / max(source_w, 1))
    work = (
        cv2.resize(frame, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
        if scale < 1.0
        else frame.copy()
    )
    h, w = work.shape[:2]
    search = config.get("search") or {}
    x0 = max(0, min(w - 1, int(w * float(search.get("x0", 0.0)))))
    y0 = max(0, min(h - 1, int(h * float(search.get("y0", 0.0)))))
    x1 = max(x0 + 1, min(w, int(w * float(search.get("x1", 1.0)))))
    y1 = max(y0 + 1, min(h, int(h * float(search.get("y1", 0.88)))))
    area = work[y0:y1, x0:x1]
    raw_mask = _color_mask(area, config)
    kernel = cv2.getStructuringElement(
        cv2.MORPH_RECT,
        (max(5, w // 90), max(1, h // 360)),
    )
    joined = cv2.morphologyEx(raw_mask, cv2.MORPH_CLOSE, kernel)
    joined = cv2.dilate(
        joined,
        cv2.getStructuringElement(cv2.MORPH_RECT, (max(3, w // 140), 2)),
        iterations=1,
    )
    count, _labels, stats, _centroids = cv2.connectedComponentsWithStats(joined)
    min_width = max(8, int(w * float(config.get("min_width_ratio", 0.035))))
    max_height = max(4, int(h * float(config.get("max_height_ratio", 0.14))))
    min_aspect = float(config.get("min_aspect_ratio", 1.4))
    min_pixels = int(config.get("min_colored_pixels", 10))
    candidates: list[dict[str, Any]] = []
    for index in range(1, count):
        x, y, width, height, _component_area = (int(value) for value in stats[index])
        if width < min_width or height < 2 or height > max_height:
            continue
        if width / max(height, 1) < min_aspect:
            continue
        colored = int(np.count_nonzero(raw_mask[y : y + height, x : x + width]))
        if colored < min_pixels:
            continue
        pad_x = max(3, int(width * 0.12))
        pad_y = max(2, int(height * 0.55))
        ax0 = max(0, x - pad_x)
        ay0 = max(0, y - pad_y)
        ax1 = min(area.shape[1], x + width + pad_x)
        ay1 = min(area.shape[0], y + height + pad_y)
        crop = area[ay0:ay1, ax0:ax1]
        gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
        edge_density = float(np.mean(cv2.Canny(gray, 50, 140) > 0))
        color_density = colored / max(width * height, 1)
        aspect = width / max(height, 1)
        geometric = (
            min(1.0, color_density / 0.18) * 0.50
            + min(1.0, edge_density / 0.16) * 0.25
            + min(1.0, max(0.0, aspect - min_aspect) / 5.0) * 0.25
        )
        candidates.append(
            {
                "box_work": (ax0 + x0, ay0 + y0, ax1 - ax0, ay1 - ay0),
                "geometric_score": geometric,
                "color_density": color_density,
                "edge_density": edge_density,
                "aspect": aspect,
                "crop": crop,
            }
        )
    candidates.sort(key=lambda row: float(row["geometric_score"]), reverse=True)
    out: list[dict[str, Any]] = []
    for row in candidates[:6]:
        text = _ocr_text(row.pop("crop")) if ocr else ""
        chars = len(re.sub(r"[^A-Za-z0-9А-Яа-я]", "", text))
        text_score = min(1.0, chars / 10.0)
        score = float(row["geometric_score"]) * 0.72 + text_score * 0.28
        bx, by, bw, bh = row.pop("box_work")
        out.append(
            {
                "box": [
                    round(bx / max(w, 1), 4),
                    round(by / max(h, 1), 4),
                    round(bw / max(w, 1), 4),
                    round(bh / max(h, 1), 4),
                ],
                "score": round(score, 4),
                "geometric_score": round(float(row["geometric_score"]), 4),
                "color_density": round(float(row["color_density"]), 4),
                "edge_density": round(float(row["edge_density"]), 4),
                "text": text,
                "chars": chars,
            }
        )
    return out


def _decode_sample_frames(
    video_path: Path,
    start_sec: float,
    duration_sec: float,
    *,
    fps: float,
    max_frames: int,
) -> list[np.ndarray]:
    import cv2

    effective_fps = min(max(0.25, fps), max_frames / max(duration_sec, 0.1))
    command = [
        "ffmpeg",
        "-v",
        "error",
        "-hwaccel",
        "none",
        "-ss",
        f"{max(0.0, start_sec):.3f}",
        "-t",
        f"{max(0.2, duration_sec):.3f}",
        "-i",
        str(video_path),
        "-vf",
        f"fps={effective_fps:.4f}",
        "-q:v",
        "5",
        "-f",
        "image2pipe",
        "-vcodec",
        "mjpeg",
        "-",
    ]
    try:
        result = subprocess.run(command, capture_output=True, check=False, timeout=120)
    except subprocess.TimeoutExpired:
        return []
    if result.returncode != 0 or not result.stdout:
        return []
    frames: list[np.ndarray] = []
    payload = result.stdout
    cursor = 0
    while len(frames) < max_frames:
        begin = payload.find(b"\xff\xd8", cursor)
        if begin < 0:
            break
        end = payload.find(b"\xff\xd9", begin + 2)
        if end < 0:
            break
        image = cv2.imdecode(
            np.frombuffer(payload[begin : end + 2], dtype=np.uint8),
            cv2.IMREAD_COLOR,
        )
        if image is not None:
            frames.append(image)
        cursor = end + 2
    return frames


def _box_iou(left: list[float] | None, right: list[float] | None) -> float:
    if not left or not right:
        return 0.0
    lx, ly, lw, lh = left
    rx, ry, rw, rh = right
    ix0, iy0 = max(lx, rx), max(ly, ry)
    ix1, iy1 = min(lx + lw, rx + rw), min(ly + lh, ry + rh)
    intersection = max(0.0, ix1 - ix0) * max(0.0, iy1 - iy0)
    union = lw * lh + rw * rh - intersection
    return intersection / max(union, 1e-9)


def score_kill_notification_segment(
    video_path: Path,
    start_sec: float,
    duration_sec: float,
) -> tuple[float, dict[str, Any]]:
    """Score a transient colored nickname notification regardless of HUD position."""
    config = load_config()
    if not config.get("enabled", True) or os.environ.get(
        "PUBG_KILL_NOTIFICATION_ENABLED", "1"
    ) != "1":
        return 0.0, {"notification_disabled": True}
    frames = _decode_sample_frames(
        video_path,
        start_sec,
        duration_sec,
        fps=float(config.get("sample_fps", 1.5)),
        max_frames=int(config.get("max_frames", 18)),
    )
    if not frames:
        return 0.0, {"notification_frames": 0, "notification_hits": 0}
    try:
        from gameplay_gate import detect_game_viewport_crop

        viewport = detect_game_viewport_crop(video_path, start_sec, duration_sec)
    except Exception:
        viewport = None
    threshold = float(config.get("min_score", 0.34))
    views: list[np.ndarray] = []
    region_rows: list[list[dict[str, Any]]] = []
    for frame in frames:
        if viewport is not None:
            x, y, width, height = (int(value) for value in viewport)
            if y + height <= frame.shape[0] and x + width <= frame.shape[1]:
                frame = frame[y : y + height, x : x + width]
        views.append(frame)
        region_rows.append(locate_notification_regions(frame, config=config, ocr=False))

    best_event: dict[str, Any] | None = None
    for index, regions in enumerate(region_rows):
        if index == 0:
            continue
        if index + 1 >= len(region_rows):
            break
        previous = region_rows[index - 1] if index > 0 else []
        following = region_rows[index + 1]
        for region in regions:
            box = region.get("box")
            prev_iou = max(
                (_box_iou(box, other.get("box")) for other in previous),
                default=0.0,
            )
            next_matches = [
                other for other in following if _box_iou(box, other.get("box")) >= 0.18
            ]
            if prev_iou >= 0.22 or not next_matches:
                continue
            next_region = max(next_matches, key=lambda row: float(row.get("score", 0.0)))
            track = [(index, region), (index + 1, next_region)]
            track_box = next_region.get("box")
            for future_index in range(index + 2, min(len(region_rows), index + 6)):
                matches = [
                    other
                    for other in region_rows[future_index]
                    if _box_iou(track_box, other.get("box")) >= 0.18
                ]
                if not matches:
                    break
                match = max(matches, key=lambda row: float(row.get("score", 0.0)))
                track.append((future_index, match))
                track_box = match.get("box")
            if len(track) < int(config.get("min_persistence_frames", 3)):
                continue
            onset = (
                float(region.get("score", 0.0)) * 0.48
                + float(next_region.get("score", 0.0)) * 0.30
                + (1.0 - prev_iou) * 0.22
            )
            onset = min(1.0, onset + min(0.08, (len(track) - 2) * 0.02))
            event = {
                "index": index,
                "score": onset,
                "box": box,
                "next_box": next_region.get("box"),
                "prev_iou": prev_iou,
                "track_frames": [frame_index for frame_index, _row in track],
            }
            if best_event is None or onset > float(best_event["score"]):
                best_event = event

    best_score = 0.0
    best_text = ""
    best_box: list[float] | None = None
    event_index = None
    if best_event is not None:
        event_index = int(best_event["index"])
        best_box = best_event.get("box")
        ocr_regions = locate_notification_regions(
            views[event_index],
            config=config,
            ocr=True,
        )
        matching = [
            row for row in ocr_regions if _box_iou(best_box, row.get("box")) >= 0.18
        ]
        ocr_region = max(
            matching or ocr_regions,
            key=lambda row: float(row.get("score", 0.0)),
            default={},
        )
        best_text = str(ocr_region.get("text") or "")
        text_chars = len(re.sub(r"[^A-Za-z0-9А-Яа-я]", "", best_text))
        text_score = min(1.0, text_chars / 10.0)
        best_score = min(
            1.0,
            float(best_event["score"]) * 0.80 + text_score * 0.20,
        )
        if text_chars == 0:
            best_score *= 0.72
        elif text_chars < 3:
            best_score *= 0.84

    frame_rows: list[dict[str, Any]] = []
    hits = 0
    for index, regions in enumerate(region_rows):
        best = regions[0] if regions else {"score": 0.0, "box": None}
        event_hit = (
            best_event is not None
            and index in (best_event.get("track_frames") or [])
        )
        hits += int(event_hit)
        frame_rows.append(
            {
                "index": index,
                "score": round(float(best.get("score", 0.0)), 4),
                "hit": event_hit,
                "text": best_text[:80] if index == event_index else "",
                "box": best.get("box"),
            }
        )
    ratio = hits / max(len(frames), 1)
    score = best_score if best_score >= threshold else best_score * 0.75
    return score, {
        "notification_score": round(score, 4),
        "notification_best_frame_score": round(best_score, 4),
        "notification_frames": len(frames),
        "notification_hits": hits,
        "notification_hit_ratio": round(ratio, 4),
        "notification_text": best_text,
        "notification_box": best_box,
        "notification_event_index": event_index,
        "notification_onset": best_event,
        "notification_samples": frame_rows,
    }


__all__ = [
    "load_config",
    "locate_notification_regions",
    "score_kill_notification_segment",
]
