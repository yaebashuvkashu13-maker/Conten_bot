#!/usr/bin/env python3
"""Build an 8s window labeling queue from a PUBG VOD (owner rates fight/not-fight).

Does NOT require watching every second of the VOD: mixes a coarse grid with
audio-dense candidates so ~N ratings cover useful diversity.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path
from typing import Any


def queue_path() -> Path:
    return Path(
        os.environ.get(
            "PUBG_WINDOW_LABEL_QUEUE",
            "/root/data/pubg/window_label_queue.jsonl",
        )
    )


def _ffprobe_duration(path: Path) -> float:
    import subprocess

    try:
        out = subprocess.check_output(
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
            text=True,
            timeout=60,
        )
        return max(0.0, float(out.strip()))
    except Exception:
        return 0.0


def _grid_peaks(duration: float, window: float, stride: float) -> list[float]:
    peaks: list[float] = []
    t = window * 0.5
    while t + window * 0.5 <= duration:
        peaks.append(round(t, 1))
        t += stride
    return peaks


def _dense_peaks(vod: Path, limit: int) -> list[float]:
    """Reuse existing dense gun cache / probe when available; else empty."""
    try:
        from shooter_vod_fast_scan import load_dense_gun_peaks_cache

        cached = load_dense_gun_peaks_cache(vod)
        if cached:
            return [float(p) for p in cached[:limit]]
    except Exception:
        pass
    try:
        # Optional live probe — can be slow; gated by env.
        if os.environ.get("PUBG_WINDOW_QUEUE_LIVE_DENSE", "0") != "1":
            return []
        from shooter_vod_fast_scan import dense_gun_peaks

        peaks = dense_gun_peaks(vod) or []
        return [float(p) for p in peaks[:limit]]
    except Exception:
        return []


def build_queue(
    vod: Path,
    *,
    window_sec: float = 8.0,
    grid_stride_sec: float = 24.0,
    max_windows: int = 120,
    video_id: str = "",
) -> dict[str, Any]:
    duration = _ffprobe_duration(vod)
    if duration < window_sec:
        return {"status": "too_short", "duration": duration, "rows": 0}

    vid = video_id or (vod.stem[3:14] if vod.stem.startswith("yt_") else vod.stem[:11])
    grid = _grid_peaks(duration, window_sec, grid_stride_sec)
    dense = _dense_peaks(vod, limit=max(40, max_windows // 2))

    # Prefer dense, then fill with grid far from already picked.
    picked: list[float] = []
    for peak in dense + grid:
        if any(abs(peak - p) < window_sec * 0.6 for p in picked):
            continue
        picked.append(peak)
        if len(picked) >= max_windows:
            break

    rows = []
    for peak in picked:
        t0 = max(0.0, peak - window_sec * 0.5)
        t1 = min(duration, t0 + window_sec)
        rows.append(
            {
                "video_id": vid,
                "video_path": str(vod),
                "t0": round(t0, 2),
                "t1": round(t1, 2),
                "peak_sec": round((t0 + t1) * 0.5, 2),
                "status": "pending",
                "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
        )

    out = queue_path()
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    return {
        "status": "queued",
        "video_id": vid,
        "duration": round(duration, 1),
        "rows": len(rows),
        "dense_candidates": len(dense),
        "grid_candidates": len(grid),
        "queue_path": str(out),
        "hint": "Rate with: python3 scripts/pubg_ranker_dataset.py add-window "
        "--video-id ID --t0 T0 --t1 T1 --label good|bad --event fight|loot|menu|other",
    }


def render_preview(vod: Path, t0: float, t1: float, dest: Path) -> Path:
    """Cut a cheap preview mp4 for Telegram rating (copy codecs when possible)."""
    import subprocess

    dest.parent.mkdir(parents=True, exist_ok=True)
    dur = max(1.0, float(t1) - float(t0))
    cmd = [
        "ffmpeg",
        "-y",
        "-ss",
        f"{float(t0):.2f}",
        "-i",
        str(vod),
        "-t",
        f"{dur:.2f}",
        "-c",
        "copy",
        "-movflags",
        "+faststart",
        str(dest),
    ]
    subprocess.run(cmd, check=False, capture_output=True, timeout=120)
    if not dest.is_file() or dest.stat().st_size < 1000:
        # Re-encode fallback for non-keyframe cuts.
        cmd = [
            "ffmpeg",
            "-y",
            "-ss",
            f"{float(t0):.2f}",
            "-i",
            str(vod),
            "-t",
            f"{dur:.2f}",
            "-vf",
            "scale=720:-2",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "28",
            "-c:a",
            "aac",
            "-b:a",
            "96k",
            str(dest),
        ]
        subprocess.run(cmd, check=True, capture_output=True, timeout=180)
    return dest


def main() -> int:
    parser = argparse.ArgumentParser(description="PUBG 15s window label queue")
    sub = parser.add_subparsers(dest="cmd", required=True)

    build = sub.add_parser("build", help="Append windows for one VOD into the queue")
    build.add_argument("--vod", type=Path, required=True)
    build.add_argument("--window-sec", type=float, default=8.0)
    build.add_argument("--stride-sec", type=float, default=24.0)
    build.add_argument("--max-windows", type=int, default=120)
    build.add_argument("--video-id", default="")

    preview = sub.add_parser("preview", help="Render one window preview mp4")
    preview.add_argument("--vod", type=Path, required=True)
    preview.add_argument("--t0", type=float, required=True)
    preview.add_argument("--t1", type=float, required=True)
    preview.add_argument("--out", type=Path, required=True)

    args = parser.parse_args()
    if args.cmd == "build":
        report = build_queue(
            args.vod,
            window_sec=args.window_sec,
            grid_stride_sec=args.stride_sec,
            max_windows=args.max_windows,
            video_id=args.video_id,
        )
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
        return 0 if report.get("status") == "queued" else 2
    if args.cmd == "preview":
        path = render_preview(args.vod, args.t0, args.t1, args.out)
        print(json.dumps({"status": "ok", "path": str(path), "bytes": path.stat().st_size}))
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
