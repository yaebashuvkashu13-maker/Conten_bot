#!/usr/bin/env python3
"""Compare PANNs inference backends on CPU (PyTorch baseline)."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))


def _sample_audio(video: Path, start: float = 60.0, dur: float = 4.0) -> np.ndarray:
    from highlight_scorer import _extract_audio_32k

    return _extract_audio_32k(video, start, dur)


def bench_pytorch(audio: np.ndarray, *, runs: int = 5) -> dict:
    from highlight_scorer import _panns_tagger

    tagger = _panns_tagger()
    times: list[float] = []
    for _ in range(runs):
        t0 = time.perf_counter()
        tagger.inference(audio[None, :])
        times.append((time.perf_counter() - t0) * 1000.0)
    times.sort()
    return {
        "backend": "pytorch",
        "p50_ms": round(times[len(times) // 2], 2),
        "p95_ms": round(times[max(0, int(len(times) * 0.95) - 1)], 2),
    }


def bench_batch(audio: np.ndarray, batch: int, *, runs: int = 3) -> dict:
    from highlight_scorer import score_panns_audio_batch

    video = Path(os.environ.get("BENCHMARK_VOD", "/dev/null"))
    if not video.is_file():
        return {"backend": f"batch_{batch}", "skipped": "no_video"}
    windows = [(60.0 + i * 4.0, 4.0) for i in range(batch)]
    times: list[float] = []
    for _ in range(runs):
        t0 = time.perf_counter()
        score_panns_audio_batch(video, windows)
        times.append((time.perf_counter() - t0) * 1000.0)
    times.sort()
    return {
        "backend": f"pytorch_batch_{batch}",
        "p50_ms": round(times[len(times) // 2], 2),
        "p95_ms": round(times[max(0, int(len(times) * 0.95) - 1)], 2),
        "per_window_ms": round(times[len(times) // 2] / batch, 2),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--vod", type=Path)
    parser.add_argument("--output", type=Path, default=Path("/root/data/pubg/model_backend_benchmark.json"))
    args = parser.parse_args()
    vod = args.vod
    if vod is None:
        for candidate in (
            Path("/root/data/pubg/regression_vods"),
            Path("/root/data/pubg/youtube_nightly/inbox"),
        ):
            if candidate.is_dir():
                files = sorted(candidate.glob("*.mp4"), key=lambda p: p.stat().st_size)
                if files:
                    vod = files[0]
                    break
    report: dict = {"vod": str(vod) if vod else None, "backends": []}
    if vod is None or not vod.is_file():
        report["error"] = "no_vod"
        print(json.dumps(report, indent=2))
        return 1
    os.environ["BENCHMARK_VOD"] = str(vod)
    audio = _sample_audio(vod)
    if audio.size == 0:
        report["error"] = "no_audio"
        print(json.dumps(report, indent=2))
        return 1
    report["backends"].append(bench_pytorch(audio))
    for batch in (4, 8):
        report["backends"].append(bench_batch(audio, batch))
    # Placeholders for future ONNX/OpenVINO — document when deps installed
    for name in ("onnxruntime", "openvino"):
        report["backends"].append({"backend": name, "status": "not_installed"})
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
