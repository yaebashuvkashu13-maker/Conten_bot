#!/usr/bin/env python3
"""MLBB kill-notification UI detector (Savage/Maniac/kill feed)."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import cv2
import numpy as np

MLBB_KILL_KEYWORDS = re.compile(
    r"(savage|maniac|legendary|triple|double|wipe|wiped|first\s*blood|"
    r"pentakill|killing\s*spree|shutdown|\bace\b|team\s*wipe|slain|"
    r"has\s*been\s*slain|\bkill(?:ed|ing)?\b)",
    re.I,
)


@dataclass(slots=True)
class KillUiResult:
    score: float
    has_kill_notification: bool
    keyword_hits: int
    announce_color_peak: float
    kill_feed_activity: float
    ocr_snippet: str
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _import_frame_helpers():
    try:
        from gameplay_gate import _read_frame_at, detect_game_viewport_crop
    except ImportError:
        scripts = Path(__file__).resolve().parent
        if str(scripts) not in sys.path:
            sys.path.insert(0, str(scripts))
        from gameplay_gate import _read_frame_at, detect_game_viewport_crop
    return _read_frame_at, detect_game_viewport_crop


def _ffmpeg_sample_frames(
    video_path: Path,
    start_sec: float,
    duration_sec: float,
    sample_count: int,
) -> list[np.ndarray]:
    if duration_sec <= 0.2:
        duration_sec = 0.5
    fps = max(0.5, sample_count / duration_sec)
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-hwaccel",
        "none",
        "-ss",
        f"{max(0.0, start_sec):.3f}",
        "-i",
        str(video_path),
        "-t",
        f"{duration_sec:.3f}",
        "-vf",
        f"fps={fps:.3f},scale=320:180",
        "-frames:v",
        str(max(1, sample_count)),
        "-f",
        "rawvideo",
        "-pix_fmt",
        "bgr24",
        "-",
    ]
    proc = subprocess.run(cmd, capture_output=True, check=False, timeout=25)
    if proc.returncode != 0 or not proc.stdout:
        return []
    frame_bytes = 320 * 180 * 3
    raw = proc.stdout
    frames: list[np.ndarray] = []
    for offset in range(0, len(raw) - frame_bytes + 1, frame_bytes):
        chunk = raw[offset : offset + frame_bytes]
        if len(chunk) < frame_bytes:
            break
        frames.append(np.frombuffer(chunk, dtype=np.uint8).reshape((180, 320, 3)).copy())
        if len(frames) >= sample_count:
            break
    return frames


def _sample_frames(
    video_path: Path,
    start_sec: float,
    duration_sec: float,
    sample_count: int,
) -> list[np.ndarray]:
    frames = _ffmpeg_sample_frames(video_path, start_sec, duration_sec, sample_count)
    if frames:
        return frames

    read_frame_at, detect_crop = _import_frame_helpers()
    crop = detect_crop(video_path, start_sec, duration_sec)
    if duration_sec <= 0.4:
        times = [start_sec]
    else:
        times = np.linspace(start_sec + 0.15, start_sec + duration_sec - 0.15, sample_count)
    fallback: list[np.ndarray] = []
    for t in times:
        frame = read_frame_at(video_path, float(t))
        if frame is None:
            continue
        if crop is not None:
            x, y, w, h = crop
            frame = frame[y : y + h, x : x + w]
        fallback.append(frame)
    return fallback


def _announce_color_score(frame: np.ndarray) -> float:
    """Gold/white kill announcement banner in the top-center zone."""
    small = cv2.resize(frame, (320, 180))
    hsv = cv2.cvtColor(small, cv2.COLOR_BGR2HSV)
    h, w = small.shape[:2]
    zone = hsv[int(h * 0.02) : int(h * 0.30), int(w * 0.18) : int(w * 0.82)]
    if zone.size == 0:
        return 0.0
    gold = cv2.inRange(zone, np.array([8, 110, 150]), np.array([38, 255, 255]))
    white = cv2.inRange(zone, np.array([0, 0, 215]), np.array([180, 45, 255]))
    combined = cv2.bitwise_or(gold, white)
    ratio = float(np.count_nonzero(combined)) / float(combined.size)
    return min(1.0, ratio * 10.0)


def _kill_feed_spike(frames: list[np.ndarray]) -> float:
    """Transient activity in the left kill-feed column (new lines appearing)."""
    if len(frames) < 2:
        return 0.0
    deltas: list[float] = []
    prev_feed: np.ndarray | None = None
    for frame in frames:
        small = cv2.resize(frame, (320, 180))
        gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
        h, w = gray.shape
        feed = gray[int(h * 0.04) : int(h * 0.42), int(w * 0.02) : int(w * 0.34)]
        if feed.size == 0:
            continue
        if prev_feed is not None and prev_feed.shape == feed.shape:
            deltas.append(float(cv2.absdiff(feed, prev_feed).mean()) / 255.0)
        prev_feed = feed
    if not deltas:
        return 0.0
    peak = float(max(deltas))
    mean = float(np.mean(deltas))
    return min(1.0, peak * 2.8 + max(0.0, peak - mean) * 1.5)


def _ocr_kill_text(frame: np.ndarray) -> tuple[str, int]:
    try:
        import pytesseract
    except ImportError:
        return "", 0

    small = cv2.resize(frame, (480, 270))
    h, w = small.shape[:2]
    zones = [
        small[int(h * 0.02) : int(h * 0.28), int(w * 0.12) : int(w * 0.88)],
        small[int(h * 0.04) : int(h * 0.40), int(w * 0.02) : int(w * 0.36)],
    ]
    merged = ""
    hits = 0
    for zone in zones:
        if zone.size == 0:
            continue
        gray = cv2.cvtColor(zone, cv2.COLOR_BGR2GRAY)
        gray = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1]
        text = pytesseract.image_to_string(
            gray,
            config="--psm 7 -c tessedit_char_whitelist=ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789! ",
        )
        text = " ".join(text.split())
        merged = f"{merged} {text}".strip()
        hits = max(hits, len(MLBB_KILL_KEYWORDS.findall(text)))
    if hits == 0 and merged:
        hits = len(MLBB_KILL_KEYWORDS.findall(merged))
    return merged[:160], hits


def score_mlbb_kill_ui(
    video_path: Path | str,
    start_sec: float,
    duration_sec: float,
    *,
    sample_frames: int | None = None,
    strict: bool = False,
) -> KillUiResult:
    video_path = Path(video_path)
    sample_n = sample_frames or int(os.environ.get("MLBB_KILL_UI_SAMPLES", "8"))
    frames = _sample_frames(video_path, start_sec, duration_sec, sample_n)
    if not frames:
        return KillUiResult(0.0, False, 0, 0.0, 0.0, "", "no_frames")

    color_scores = [_announce_color_score(frame) for frame in frames]
    announce_peak = float(max(color_scores))
    announce_mean = float(np.mean(color_scores)) if color_scores else 0.0
    announce_spike = max(0.0, announce_peak - announce_mean)
    feed_peak = _kill_feed_spike(frames)

    ocr_text = ""
    keyword_hits = 0
    skip_ocr = os.environ.get("MLBB_KILL_UI_SKIP_OCR", "0") == "1" and not strict
    ocr_trigger = (announce_peak >= 0.04 or feed_peak >= 0.05 or strict) and not skip_ocr
    if ocr_trigger or os.environ.get("MLBB_KILL_UI_ALWAYS_OCR", "0") == "1":
        for frame in frames:
            text, hits = _ocr_kill_text(frame)
            if text:
                ocr_text = text
            keyword_hits = max(keyword_hits, hits)

    score = announce_spike * 0.55 + announce_peak * 0.20 + feed_peak * 0.15 + min(keyword_hits * 0.18, 0.30)
    min_score = float(os.environ.get("MLBB_KILL_UI_MIN_SCORE", "0.14"))
    min_color = float(os.environ.get("MLBB_KILL_ANNOUNCE_MIN", "0.08"))
    min_spike = float(os.environ.get("MLBB_KILL_ANNOUNCE_SPIKE_MIN", "0.075"))
    min_feed = float(os.environ.get("MLBB_KILL_FEED_MIN", "0.30"))

    if strict:
        # Send gate: must read kill text (Savage/Maniac/slain/etc.) on screen.
        has_kill = keyword_hits > 0 and score >= min_score * 0.5
    else:
        has_kill = keyword_hits > 0 or (
            announce_peak >= min_color
            and announce_spike >= min_spike
            and feed_peak >= min_feed
            and score >= min_score * 0.75
        )
    has_kill = has_kill and (keyword_hits > 0 or score >= min_score * 0.65)

    if keyword_hits:
        reason = f"kill_keyword={keyword_hits}"
    elif announce_peak >= min_color and announce_spike >= min_spike:
        reason = f"announce_spike={announce_spike:.3f}:peak{announce_peak:.3f}"
    elif feed_peak >= min_feed:
        reason = f"kill_feed={feed_peak:.3f}"
    else:
        reason = "no_kill_ui"

    return KillUiResult(
        score=round(min(1.0, score), 4),
        has_kill_notification=has_kill,
        keyword_hits=keyword_hits,
        announce_color_peak=round(announce_peak, 4),
        kill_feed_activity=round(feed_peak, 4),
        ocr_snippet=ocr_text,
        reason=reason,
    )


def combat_override_allowed(
    *,
    center_motion: float,
    skill_delta: float,
    minimap_delta: float,
) -> bool:
    return (
        center_motion >= float(os.environ.get("MLBB_KILL_OVERRIDE_MOTION", "0.038"))
        and skill_delta >= float(os.environ.get("MLBB_KILL_OVERRIDE_SKILL", "0.015"))
        and minimap_delta >= float(os.environ.get("MLBB_KILL_OVERRIDE_MINI", "0.017"))
    )


def passes_mlbb_kill_gate(
    video_path: Path | str,
    start_sec: float,
    duration_sec: float,
    *,
    center_motion: float = 0.0,
    skill_delta: float = 0.0,
    minimap_delta: float = 0.0,
) -> tuple[bool, str, KillUiResult]:
    if os.environ.get("MLBB_REQUIRE_KILL_UI", "1") != "1":
        dummy = KillUiResult(1.0, True, 0, 0.0, 0.0, "", "disabled")
        return True, "kill_ui_disabled", dummy

    video_path = Path(video_path)
    motion = center_motion
    skill = skill_delta
    mini = minimap_delta
    if motion <= 0.0 and skill <= 0.0 and mini <= 0.0:
        try:
            from gameplay_gate import detect_game_viewport_crop, score_segment_combat

            crop = detect_game_viewport_crop(video_path, start_sec, duration_sec)
            motion, mini, skill, _text = score_segment_combat(
                video_path, start_sec, duration_sec, crop_box=crop, sample_frames=6
            )
        except ImportError:
            pass

    idle_motion = float(os.environ.get("MLBB_FIGHT_IDLE_MOTION", "0.028"))
    min_motion = float(os.environ.get("MLBB_FIGHT_MIN_MOTION", "0.038"))
    min_skill = float(os.environ.get("MLBB_FIGHT_MIN_SKILL", "0.014"))
    min_mini = float(os.environ.get("MLBB_FIGHT_MIN_MINIMAP", "0.016"))

    if motion < idle_motion:
        result = score_mlbb_kill_ui(video_path, start_sec, duration_sec, strict=True, sample_frames=5)
        return False, f"idle_motion={motion:.3f}", result
    if motion < min_motion or (skill < min_skill and mini < min_mini):
        result = score_mlbb_kill_ui(video_path, start_sec, duration_sec, strict=True, sample_frames=5)
        return False, f"laning_motion={motion:.3f}:skill{skill:.3f}:mini{mini:.3f}", result

    result = score_mlbb_kill_ui(video_path, start_sec, duration_sec, strict=True, sample_frames=6)
    if not result.has_kill_notification:
        return False, result.reason, result
    return True, result.reason, result


def filter_starts_by_kill_ui(
    video_path: Path | str,
    starts: list[float],
    *,
    window_sec: float | None = None,
    limit: int | None = None,
) -> list[float]:
    """Keep only windows that show MLBB kill UI (fast second pass on sparse peaks)."""
    video_path = Path(video_path)
    window_sec = window_sec or float(os.environ.get("MLBB_KILL_SCAN_WINDOW_SEC", "12"))
    limit = limit or int(os.environ.get("MLBB_KILL_SCAN_LIMIT", "32"))
    prev_skip = os.environ.get("MLBB_KILL_UI_SKIP_OCR")
    os.environ["MLBB_KILL_UI_SKIP_OCR"] = "1"
    kept: list[tuple[float, float]] = []
    try:
        for start in starts:
            ok, _reason, result = passes_mlbb_kill_gate(video_path, float(start), window_sec)
            if ok:
                kept.append((float(start), result.score))
    finally:
        if prev_skip is None:
            os.environ.pop("MLBB_KILL_UI_SKIP_OCR", None)
        else:
            os.environ["MLBB_KILL_UI_SKIP_OCR"] = prev_skip
    kept.sort(key=lambda row: row[1], reverse=True)
    return [round(row[0], 1) for row in kept[:limit]]


def scan_vod_kill_peaks(
    video_path: Path | str,
    *,
    window_sec: float | None = None,
    step_sec: float | None = None,
    min_peak_sec: float | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Fast kill-UI guided scan for long MLBB VODs."""
    video_path = Path(video_path)
    window_sec = window_sec or float(os.environ.get("MLBB_KILL_SCAN_WINDOW_SEC", "12"))
    step_sec = step_sec or float(os.environ.get("MLBB_KILL_SCAN_STEP_SEC", "45"))
    min_peak_sec = min_peak_sec or float(os.environ.get("MLBB_VOD_MIN_PEAK_SEC", "420"))
    limit = limit or int(os.environ.get("MLBB_KILL_SCAN_LIMIT", "48"))

    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        return []
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    cap.release()
    duration = frame_count / fps if frame_count > 0 else 0.0
    if duration <= window_sec + 1.0:
        return []

    peaks: list[dict[str, Any]] = []
    start = min_peak_sec
    while start + window_sec <= duration - 20.0 and len(peaks) < limit:
        result = score_mlbb_kill_ui(video_path, start, window_sec, sample_frames=5)
        if result.has_kill_notification:
            peaks.append(
                {
                    "start_sec": round(start, 2),
                    "duration_sec": round(window_sec, 2),
                    **result.to_dict(),
                }
            )
        start += step_sec
    peaks.sort(key=lambda row: row["score"], reverse=True)
    return peaks[:limit]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Score MLBB kill UI in a video window.")
    parser.add_argument("video", type=Path)
    parser.add_argument("--start", type=float, default=0.0)
    parser.add_argument("--duration", type=float, default=12.0)
    parser.add_argument("--scan-vod", action="store_true")
    parser.add_argument("--limit", type=int, default=20)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.scan_vod:
        rows = scan_vod_kill_peaks(args.video, limit=args.limit)
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0
    result = score_mlbb_kill_ui(args.video, args.start, args.duration)
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
