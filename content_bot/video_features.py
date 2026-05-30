from __future__ import annotations

import argparse
import csv
from pathlib import Path

import cv2
import numpy as np


def extract_video_features(video_path: str | Path, sample_fps: float = 4.0) -> dict[str, float]:
    path = Path(video_path)
    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        raise RuntimeError(f"Cannot open video: {path}")

    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    sample_every = max(int(round(fps / sample_fps)), 1)

    motion_values: list[float] = []
    center_motion_values: list[float] = []
    brightness_values: list[float] = []
    saturation_values: list[float] = []
    sharpness_values: list[float] = []
    center_h_hist_acc = np.zeros(8, dtype=np.float64)
    center_s_hist_acc = np.zeros(8, dtype=np.float64)
    center_v_hist_acc = np.zeros(8, dtype=np.float64)
    hist_frames = 0

    prev_gray = None
    frame_idx = 0

    while True:
        ok, frame = cap.read()
        if not ok:
            break
        if frame_idx % sample_every != 0:
            frame_idx += 1
            continue

        frame_small = cv2.resize(frame, (160, 90), interpolation=cv2.INTER_AREA)
        gray = cv2.cvtColor(frame_small, cv2.COLOR_BGR2GRAY)
        hsv = cv2.cvtColor(frame_small, cv2.COLOR_BGR2HSV)
        h, w = gray.shape
        y0, y1 = int(h * 0.22), int(h * 0.78)
        x0, x1 = int(w * 0.18), int(w * 0.82)
        center_hsv = hsv[y0:y1, x0:x1]

        brightness_values.append(float(gray.mean() / 255.0))
        saturation_values.append(float(hsv[..., 1].mean() / 255.0))
        sharpness_values.append(float(cv2.Laplacian(gray, cv2.CV_32F).var()))
        center_h_hist_acc += cv2.calcHist([center_hsv], [0], None, [8], [0, 180]).flatten()
        center_s_hist_acc += cv2.calcHist([center_hsv], [1], None, [8], [0, 256]).flatten()
        center_v_hist_acc += cv2.calcHist([center_hsv], [2], None, [8], [0, 256]).flatten()
        hist_frames += 1

        if prev_gray is not None:
            diff = cv2.absdiff(gray, prev_gray)
            motion_values.append(float(diff.mean() / 255.0))
            center_motion_values.append(float(diff[y0:y1, x0:x1].mean() / 255.0))

        prev_gray = gray
        frame_idx += 1

    cap.release()

    duration = frame_idx / fps if fps else 0.0

    def stats(values: list[float], prefix: str) -> dict[str, float]:
        if not values:
            return {f"{prefix}_mean": 0.0, f"{prefix}_std": 0.0, f"{prefix}_max": 0.0}
        arr = np.array(values, dtype=np.float32)
        return {
            f"{prefix}_mean": float(arr.mean()),
            f"{prefix}_std": float(arr.std()),
            f"{prefix}_max": float(arr.max()),
        }

    features = {"duration_sec": float(duration)}
    for prefix, values in (
        ("motion", motion_values),
        ("center_motion", center_motion_values),
        ("brightness", brightness_values),
        ("saturation", saturation_values),
        ("sharpness", sharpness_values),
    ):
        features.update(stats(values, prefix))

    if hist_frames:
        h_hist = center_h_hist_acc / center_h_hist_acc.sum()
        s_hist = center_s_hist_acc / center_s_hist_acc.sum()
        v_hist = center_v_hist_acc / center_v_hist_acc.sum()
        for idx, value in enumerate(h_hist):
            features[f"center_h_hist_{idx}"] = float(value)
        for idx, value in enumerate(s_hist):
            features[f"center_s_hist_{idx}"] = float(value)
        for idx, value in enumerate(v_hist):
            features[f"center_v_hist_{idx}"] = float(value)
    return features


def build_feature_csv(input_dir: str | Path, output_csv: str | Path, label: str) -> int:
    source_dir = Path(input_dir)
    rows = []
    for video_file in sorted(source_dir.rglob("*.mp4")):
        features = extract_video_features(video_file)
        rows.append({"path": str(video_file), "label": label, **features})

    if not rows:
        return 0

    output_path = Path(output_csv)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    return len(rows)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Extract simple video features for ML training.")
    parser.add_argument("--input-dir", required=True)
    parser.add_argument("--output-csv", required=True)
    parser.add_argument("--label", required=True)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    count = build_feature_csv(args.input_dir, args.output_csv, args.label)
    print(f"Extracted features for {count} videos.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

