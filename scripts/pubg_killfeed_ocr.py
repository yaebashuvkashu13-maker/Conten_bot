#!/usr/bin/env python3
"""PUBG/Standoff killfeed OCR — bonus combat signal (not sole gate)."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import numpy as np

KILL_PATTERNS = (
    (re.compile(r"\bheadshot\b", re.I), 1.4),
    (re.compile(r"\bace\b", re.I), 1.5),
    (re.compile(r"\bclutch\b", re.I), 1.3),
    (re.compile(r"\bknock(?:ed|out)?\b", re.I), 1.1),
    (re.compile(r"\bkill\b|eliminated|убил|убийств", re.I), 1.0),
)

_DEFAULT_CROP = {"y0": 0.02, "y1": 0.22, "x0": 0.62, "x1": 0.98}


def _repo_root() -> Path:
    return Path(os.environ.get("CONTENT_BOT_REPO", "/root/content_bot_ml"))


def killfeed_crop(profile: str) -> dict[str, float]:
    profile = profile.strip().lower()
    if profile == "standoff":
        cfg_path = _repo_root() / "config" / "standoff_killfeed.json"
        if cfg_path.exists():
            try:
                data = json.loads(cfg_path.read_text(encoding="utf-8"))
                crop = data.get("crop") or data
                return {
                    "y0": float(crop.get("y0", 0.03)),
                    "y1": float(crop.get("y1", 0.20)),
                    "x0": float(crop.get("x0", 0.58)),
                    "x1": float(crop.get("x1", 0.99)),
                }
            except (json.JSONDecodeError, OSError, TypeError, ValueError):
                pass
    return dict(_DEFAULT_CROP)


def _ocr_zone_text(frame: np.ndarray, crop: dict[str, float]) -> str:
    import cv2

    try:
        import pytesseract
    except ImportError:
        return ""
    small = cv2.resize(frame, (320, 180))
    h, w = small.shape[:2]
    zone = small[
        int(h * crop["y0"]) : int(h * crop["y1"]),
        int(w * crop["x0"]) : int(w * crop["x1"]),
    ]
    if zone.size == 0:
        return ""
    gray = cv2.cvtColor(zone, cv2.COLOR_BGR2GRAY)
    gray = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1]
    text = pytesseract.image_to_string(
        gray,
        config="--psm 6 -c tessedit_char_whitelist=ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+- ",
    )
    return " ".join(text.split())


def score_killfeed_text(text: str) -> tuple[float, list[str]]:
    blob = " ".join(str(text or "").split())
    if not blob:
        return 0.0, []
    hits: list[str] = []
    score = 0.0
    for pat, weight in KILL_PATTERNS:
        if pat.search(blob):
            hits.append(pat.pattern[:24])
            score += weight
    return min(1.0, score / 3.0), hits


def score_killfeed_segment(
    video_path: Path,
    start_sec: float,
    duration_sec: float,
    profile: str,
) -> tuple[float, dict]:
    from gameplay_gate import _read_frame_at, detect_game_viewport_crop

    crop = killfeed_crop(profile)
    viewport = detect_game_viewport_crop(video_path, start_sec, duration_sec)
    merged = ""
    density = 0.0
    tags: list[str] = []
    samples = 0
    for frac in (0.2, 0.5, 0.8):
        frame = _read_frame_at(video_path, start_sec + duration_sec * frac)
        if frame is None:
            continue
        if viewport is not None:
            x, y, w, h = viewport
            frame = frame[y : y + h, x : x + w]
        text = _ocr_zone_text(frame, crop)
        merged = f"{merged} {text}".strip()
        sc, hits = score_killfeed_text(text)
        density = max(density, sc)
        tags.extend(hits)
        samples += 1
    if samples == 0:
        return 0.0, {"killfeed_text": "", "killfeed_hits": []}
    full_sc, full_hits = score_killfeed_text(merged)
    density = max(density, full_sc)
    tags = sorted(set(tags + full_hits))
    return density, {"killfeed_text": merged[:160], "killfeed_hits": tags, "killfeed_density": density}


def rank_peaks_by_killfeed(
    video_path: Path,
    peaks: list[float],
    profile: str,
    *,
    part_sec: float = 14.0,
    max_probes: int = 12,
) -> tuple[list[float], str]:
    """Reorder peak shortlist — killfeed OCR first (cheap, before presend gates)."""
    if os.environ.get("PUBG_KILLFEED_RANK", "1") != "1":
        return list(peaks), "killfeed_rank_off"
    if not peaks:
        return [], "killfeed_rank_empty"
    profile = profile.strip().lower()
    if profile not in ("pubg", "standoff"):
        return list(peaks), "killfeed_rank_skip_profile"

    cap = max(2, int(os.environ.get("PUBG_KILLFEED_RANK_MAX", str(max_probes))))
    probe = list(peaks)[:cap]
    scored: list[tuple[float, float, float]] = []  # killfeed, panns_order, peak
    for i, peak in enumerate(probe):
        start = max(0.0, float(peak) - part_sec * 0.5)
        try:
            kf, _meta = score_killfeed_segment(video_path, start, part_sec, profile)
        except Exception:
            kf = 0.0
        scored.append((float(kf), -float(i), float(peak)))

    scored.sort(key=lambda x: (-x[0], x[1]))
    ranked = [p for _kf, _ord, p in scored]
    tail = [p for p in peaks if p not in ranked]
    ranked.extend(tail)
    top_kf = scored[0][0] if scored else 0.0
    return ranked, f"killfeed_rank top={top_kf:.2f} n={len(probe)}"
