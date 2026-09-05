#!/usr/bin/env python3
"""Benchmark PANNs workers by final accepted PUBG clips per wall-clock minute."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


def _duration(path: Path) -> float:
    from smart_video_editor import ffprobe_duration

    return float(ffprobe_duration(path))


def _offsets(duration: float, step: float, window: float) -> list[float]:
    start = min(120.0, max(15.0, duration * 0.03))
    out: list[float] = []
    while start + window < duration - 10:
        out.append(round(start, 2))
        start += step
    return out


def run_child(
    vod: Path,
    *,
    workers: int,
    step_sec: float,
    window_sec: float,
    max_quality: int,
    limit_sec: float,
) -> dict:
    from highlight_scorer import score_panns_audio
    from pubg_quality_score import score_pubg_window

    source_duration = _duration(vod)
    duration = min(source_duration, limit_sec) if limit_sec > 0 else source_duration
    offsets = _offsets(duration, step_sec, window_sec)
    started = time.monotonic()

    def probe(start: float) -> tuple[float, float]:
        row = score_panns_audio(vod, start, window_sec)
        return start, float(row.get("panns_gun_max", 0.0))

    if workers == 1:
        scored = [probe(start) for start in offsets]
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            scored = list(pool.map(probe, offsets))
    threshold = float(os.environ.get("SHOOTER_VOD_DENSE_PANN_MIN", "0.16"))
    candidates = sorted(
        (row for row in scored if row[1] >= threshold),
        key=lambda row: row[1],
        reverse=True,
    )
    accepted = 0
    quality_rows: list[dict] = []
    for start, panns in candidates[:max_quality]:
        clip_start = max(0.0, start + window_sec * 0.5 - 7.0)
        ok, reason, report = score_pubg_window(vod, clip_start, 14.0)
        accepted += int(ok)
        quality_rows.append(
            {
                "start": round(clip_start, 2),
                "panns": round(panns, 4),
                "accepted": ok,
                "reason": reason,
                "quality_score": report.get("quality_score"),
            }
        )
    elapsed = time.monotonic() - started
    return {
        "workers": workers,
        "vod": str(vod),
        "duration_sec": round(duration, 2),
        "source_duration_sec": round(source_duration, 2),
        "probe_step_sec": step_sec,
        "probe_windows": len(offsets),
        "panns_candidates": len(candidates),
        "quality_checked": len(quality_rows),
        "accepted_clips": accepted,
        "wall_sec": round(elapsed, 2),
        "accepted_per_wall_min": round(accepted / max(elapsed / 60.0, 1e-9), 4),
        "quality_rows": quality_rows,
    }


def run_parent(args: argparse.Namespace) -> dict:
    root = Path(tempfile.mkdtemp(prefix="panns-workers-benchmark-"))
    results: list[dict] = []
    try:
        for workers in args.workers:
            cache = root / f"workers-{workers}"
            env = dict(os.environ)
            env.update(
                {
                    "PANN_AUDIO_CACHE": "1",
                    "PANN_AUDIO_CACHE_DIR": str(cache),
                    "HIGHLIGHT_PARALLEL_WORKERS": str(workers),
                    "OMP_NUM_THREADS": "1",
                    "OPENBLAS_NUM_THREADS": "1",
                    "MKL_NUM_THREADS": "1",
                }
            )
            command = [
                sys.executable,
                str(Path(__file__).resolve()),
                "--child",
                "--vod",
                str(args.vod),
                "--child-workers",
                str(workers),
                "--step-sec",
                str(args.step_sec),
                "--window-sec",
                str(args.window_sec),
                "--max-quality",
                str(args.max_quality),
                "--limit-sec",
                str(args.limit_sec),
            ]
            proc = subprocess.run(
                command,
                env=env,
                text=True,
                capture_output=True,
                timeout=args.timeout_sec,
            )
            if proc.returncode != 0:
                results.append(
                    {
                        "workers": workers,
                        "error": (proc.stderr or proc.stdout)[-2000:],
                        "returncode": proc.returncode,
                    }
                )
                continue
            line = next(
                (line for line in reversed(proc.stdout.splitlines()) if line.startswith("{")),
                "",
            )
            results.append(json.loads(line))
    finally:
        shutil.rmtree(root, ignore_errors=True)
    valid = [row for row in results if "accepted_per_wall_min" in row]
    winner = max(valid, key=lambda row: row["accepted_per_wall_min"])["workers"] if valid else None
    return {
        "benchmark": "panns_workers_v1",
        "metric": "accepted_clips / wall_clock_minute",
        "winner": winner,
        "results": results,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--vod", type=Path, required=True)
    parser.add_argument("--workers", type=int, nargs="+", default=[1, 2, 4])
    parser.add_argument("--step-sec", type=float, default=60.0)
    parser.add_argument("--window-sec", type=float, default=8.0)
    parser.add_argument("--max-quality", type=int, default=16)
    parser.add_argument("--limit-sec", type=float, default=5400.0)
    parser.add_argument("--timeout-sec", type=int, default=7200)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--child-workers", type=int, default=1, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.child:
        report = run_child(
            args.vod,
            workers=max(1, args.child_workers),
            step_sec=max(5.0, args.step_sec),
            window_sec=max(2.0, args.window_sec),
            max_quality=max(1, args.max_quality),
            limit_sec=max(0.0, args.limit_sec),
        )
    else:
        report = run_parent(args)
    text = json.dumps(report, ensure_ascii=False, indent=None if args.child else 2)
    print(text)
    if args.output and not args.child:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
