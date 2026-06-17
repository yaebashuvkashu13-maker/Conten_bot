#!/usr/bin/env python3
"""Minimal Shorts montage — trim dead head/tail before Telegram send."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


def _ffprobe_duration(path: Path) -> float:
    proc = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )
    try:
        return float((proc.stdout or "0").strip() or 0)
    except ValueError:
        return 0.0


def mini_montage_enabled() -> bool:
    return os.environ.get("MLBB_SHORTS_MINI_MONTAGE", "1") == "1"


def send_max_sec() -> float:
    return float(os.environ.get("MLBB_SHORTS_SEND_MAX_SEC", "28"))


def send_min_sec() -> float:
    return float(os.environ.get("MLBB_SHORTS_SEND_MIN_SEC", "8"))


def compute_send_duration(
    path: Path,
    start_sec: float,
    *,
    timeline: list[tuple[float, float]] | None = None,
) -> tuple[float, str]:
    """
    How many seconds to keep after start_sec.
    Trims tail when action drops; caps length for faster owner review.
    """
    dur = _ffprobe_duration(path)
    if dur <= 0:
        return 0.0, "no_duration"
    remain = max(0.0, dur - start_sec)
    max_out = send_max_sec()
    min_out = send_min_sec()

    if os.environ.get("MLBB_SHORTS_TRIM_TAIL", "1") != "1":
        out = min(remain, max_out)
        return max(min_out, out) if out >= min_out else out, "cap_only"

    if timeline is None:
        try:
            from mlbb_youtube_shorts_ingest import _action_timeline_scores

            timeline = _action_timeline_scores(path)
        except Exception:
            timeline = []

    window = float(os.environ.get("MLBB_TAIL_WINDOW_SEC", "4.0"))
    threshold = float(os.environ.get("MLBB_TAIL_TRIM_MIN_SCORE", "0.018"))
    tail_pad = float(os.environ.get("MLBB_TAIL_TRIM_PAD_SEC", "1.0"))

    last_active_end = start_sec + min_out
    for t, score in timeline or []:
        if t + 0.05 < start_sec:
            continue
        if score >= threshold:
            last_active_end = max(last_active_end, t + window)

    end = min(dur, last_active_end + tail_pad, start_sec + max_out)
    out_dur = end - start_sec
    if out_dur < min_out:
        out_dur = min(remain, max_out)
        return max(min_out, out_dur) if remain >= min_out else remain, "min_cap"
    return out_dur, "tail_trim"


def build_ffmpeg_filters(out_dur: float) -> tuple[str, str]:
    """Optional fade in/out — subtle, for trimmed calibration sends."""
    if not mini_montage_enabled():
        return "", ""
    fade_in = float(os.environ.get("MLBB_SHORTS_FADE_IN_SEC", "0.12"))
    fade_out = float(os.environ.get("MLBB_SHORTS_FADE_OUT_SEC", "0.15"))
    if out_dur <= fade_in + fade_out + 1.0:
        return "", ""
    fade_out_start = max(0.0, out_dur - fade_out)
    vf = f"fade=t=in:st=0:d={fade_in:.3f},fade=t=out:st={fade_out_start:.3f}:d={fade_out:.3f}"
    af = (
        f"afade=t=in:st=0:d={min(fade_in, 0.1):.3f},"
        f"afade=t=out:st={max(0.0, fade_out_start):.3f}:d={min(fade_out, 0.12):.3f}"
    )
    return vf, af
