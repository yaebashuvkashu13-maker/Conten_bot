#!/usr/bin/env python3
"""Verify PUBG segments are Metro Royale gameplay — not classic Erangel/Livik."""

from __future__ import annotations

import os
import re
from pathlib import Path

import cv2
import numpy as np

from gameplay_gate import _read_frame_at

METRO_UI_RE = re.compile(
    r"metro[\s_-]*royale|metroroyale|метро[\s_-]*роял|метророял",
    re.I,
)
CLASSIC_MAP_RE = re.compile(
    r"\b(erangel|livik|sanhok|miramar|vikendi|nusa|rondo|classic ranked|"
    r"эрангель|ливик|санhok)\b",
    re.I,
)
CLASSIC_MODE_UI_RE = re.compile(
    r"\b(classic|ranked match|unranked|tdm|team deathmatch)\b",
    re.I,
)


def _ocr_zone_text(frame: np.ndarray, *, y0: float, y1: float, x0: float, x1: float) -> str:
    try:
        import pytesseract
    except ImportError:
        return ""
    small = cv2.resize(frame, (320, 180))
    h, w = small.shape[:2]
    zone = small[int(h * y0) : int(h * y1), int(w * x0) : int(w * x1)]
    if zone.size == 0:
        return ""
    gray = cv2.cvtColor(zone, cv2.COLOR_BGR2GRAY)
    gray = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1]
    text = pytesseract.image_to_string(gray, config="--psm 6")
    return " ".join(text.split())


def _frame_sky_ratio(frame: np.ndarray) -> float:
    """Bright blue sky in upper third — classic outdoor PUBG, rare in Metro tunnels."""
    small = cv2.resize(frame, (320, 180))
    top = small[: int(180 * 0.32), :]
    hsv = cv2.cvtColor(top, cv2.COLOR_BGR2HSV)
    blue = (hsv[:, :, 0] > 88) & (hsv[:, :, 0] < 132) & (hsv[:, :, 1] > 35) & (hsv[:, :, 2] > 70)
    bright_sky = (hsv[:, :, 2] > 165) & (hsv[:, :, 1] < 90)
    mask = blue | bright_sky
    return float(np.count_nonzero(mask)) / float(mask.size or 1)


def _frame_mean_brightness(frame: np.ndarray) -> float:
    small = cv2.resize(frame, (160, 90))
    gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
    return float(np.mean(gray)) / 255.0


def _ocr_metro_signals(frame: np.ndarray) -> tuple[bool, bool]:
    """Return (metro_keyword, classic_map_keyword) from HUD zones."""
    zones = (
        (0.0, 0.16, 0.12, 0.88),
        (0.72, 0.98, 0.02, 0.42),
        (0.0, 0.22, 0.55, 0.98),
    )
    blob = " ".join(_ocr_zone_text(frame, y0=a, y1=b, x0=c, x1=d) for a, b, c, d in zones).lower()
    metro = bool(METRO_UI_RE.search(blob))
    classic = bool(CLASSIC_MAP_RE.search(blob) or CLASSIC_MODE_UI_RE.search(blob))
    return metro, classic


def segment_looks_metro_royale(
    video_path: Path,
    start_sec: float,
    duration_sec: float,
) -> tuple[bool, str]:
    if os.environ.get("PUBG_METRO_GATE", "1") != "1":
        return True, "metro_gate_off"

    end = start_sec + max(duration_sec, 1.0)
    times = [
        start_sec + 0.25,
        start_sec + duration_sec * 0.5,
        max(start_sec + 0.3, end - 0.35),
    ]
    outdoor_votes = 0
    metro_ui_votes = 0
    classic_ui_votes = 0
    dark_votes = 0
    checked = 0

    for t in times:
        frame = _read_frame_at(video_path, t)
        if frame is None:
            continue
        checked += 1
        sky = _frame_sky_ratio(frame)
        bright = _frame_mean_brightness(frame)
        metro_ui, classic_ui = _ocr_metro_signals(frame)
        if sky >= float(os.environ.get("PUBG_METRO_MAX_SKY_RATIO", "0.11")):
            outdoor_votes += 1
        if bright <= float(os.environ.get("PUBG_METRO_MAX_BRIGHTNESS", "0.42")):
            dark_votes += 1
        if metro_ui:
            metro_ui_votes += 1
        if classic_ui:
            classic_ui_votes += 1

    if checked == 0:
        return False, "metro_no_frames"

    if classic_ui_votes >= 1:
        return False, f"classic_map_ui={classic_ui_votes}"
    if outdoor_votes >= 2:
        return False, f"classic_outdoor_sky={outdoor_votes}/{checked}"
    if metro_ui_votes >= 1:
        return True, "metro_ui_ok"
    if dark_votes >= 2 and outdoor_votes == 0:
        return True, "metro_underground"
    return False, f"not_metro=sky{outdoor_votes}:dark{dark_votes}:ui{metro_ui_votes}"


def vod_looks_metro_royale(video_path: Path, *, duration_sec: float | None = None) -> tuple[bool, str]:
    """Sample a few points in the VOD before investing in a full scan."""
    if os.environ.get("PUBG_METRO_GATE", "1") != "1":
        return True, "metro_gate_off"
    if duration_sec is None:
        from mlbb_vod_segment_feed import _ffprobe_duration

        duration_sec = _ffprobe_duration(video_path)
    intro = float(os.environ.get("PUBG_METRO_VOD_SKIP_INTRO_SEC", "75"))
    dur = max(float(duration_sec or 0), intro + 30)
    probes = [intro + 15, intro + 90, min(dur * 0.45, intro + 240)]
    oks = 0
    reasons: list[str] = []
    for t in probes:
        ok, reason = segment_looks_metro_royale(video_path, t, 8.0)
        reasons.append(f"{int(t)}s:{reason}")
        if ok:
            oks += 1
    need = int(os.environ.get("PUBG_METRO_VOD_MIN_PROBES", "2"))
    if oks >= need:
        return True, f"metro_vod_ok={oks}/{len(probes)}"
    return False, f"metro_vod_reject={oks}/{len(probes)} ({';'.join(reasons[:3])})"
