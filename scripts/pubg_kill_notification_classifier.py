#!/usr/bin/env python3
"""Lightweight kill-notification classifier — heuristic + optional sklearn model."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import cv2
import numpy as np

MODEL_PATH = Path(
    os.environ.get(
        "PUBG_KILL_NOTIFICATION_CLASSIFIER",
        "/root/data/pubg/kill_notification_classifier.joblib",
    )
)


def _features(crop: np.ndarray) -> np.ndarray:
    if crop is None or crop.size == 0:
        return np.zeros(12, dtype=np.float32)
    h, w = crop.shape[:2]
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    blue = cv2.inRange(hsv, np.array([90, 40, 60]), np.array([135, 255, 255]))
    cyan = cv2.inRange(hsv, np.array([78, 40, 80]), np.array([105, 255, 255]))
    colored = cv2.bitwise_or(blue, cyan)
    colored_ratio = float(np.count_nonzero(colored)) / max(colored.size, 1)
    aspect = w / max(h, 1)
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    edges = cv2.Canny(gray, 60, 150)
    edge_ratio = float(np.count_nonzero(edges)) / max(edges.size, 1)
    mean_b = float(np.mean(crop[:, :, 0]))
    mean_g = float(np.mean(crop[:, :, 1]))
    mean_r = float(np.mean(crop[:, :, 2]))
    return np.array(
        [
            colored_ratio,
            aspect,
            edge_ratio,
            mean_b / 255.0,
            mean_g / 255.0,
            mean_r / 255.0,
            w / 1920.0,
            h / 1080.0,
            min(1.0, w * h / 40000.0),
            float(np.std(gray)) / 128.0,
            float(np.mean(hsv[:, :, 1])) / 255.0,
            float(np.mean(hsv[:, :, 2])) / 255.0,
        ],
        dtype=np.float32,
    )


def heuristic_predict(crop: np.ndarray) -> tuple[str, float]:
    feat = _features(crop)
    colored, aspect, edge = float(feat[0]), float(feat[1]), float(feat[2])
    if colored < 0.02:
        return "hud_fp", 0.15
    if aspect < 2.5:
        return "map_blue", 0.25
    if colored > 0.08 and aspect >= 3.0 and edge > 0.04:
        return "kill", min(0.92, 0.45 + colored * 2.0 + edge)
    if colored > 0.05:
        return "uncertain", 0.40 + colored
    return "hud_fp", 0.20


def _load_model() -> Any | None:
    if not MODEL_PATH.is_file():
        return None
    try:
        import joblib

        return joblib.load(MODEL_PATH)
    except Exception:
        return None


def predict(crop: np.ndarray) -> tuple[str, float]:
    model = _load_model()
    if model is None:
        return heuristic_predict(crop)
    feat = _features(crop).reshape(1, -1)
    try:
        proba = model.predict_proba(feat)[0]
        classes = list(model.classes_)
        best = int(np.argmax(proba))
        return str(classes[best]), float(proba[best])
    except Exception:
        return heuristic_predict(crop)


def train_from_manifest(manifest_path: Path | None = None) -> dict[str, Any]:
    from pubg_kill_notification_dataset import LABELS, load_manifest

    rows = load_manifest()
    labeled = [r for r in rows if r.get("label") in LABELS and r.get("label") != "uncertain"]
    if len(labeled) < 20:
        return {"status": "insufficient_data", "labeled": len(labeled), "need": 20}
    import joblib
    from sklearn.linear_model import LogisticRegression
    from sklearn.model_selection import cross_val_score

    xs: list[np.ndarray] = []
    ys: list[str] = []
    for row in labeled:
        path = Path(row.get("path") or "")
        if not path.is_file():
            continue
        crop = cv2.imread(str(path))
        if crop is None:
            continue
        xs.append(_features(crop))
        ys.append(str(row["label"]))
    if len(xs) < 20:
        return {"status": "insufficient_files", "labeled": len(xs)}
    X = np.vstack(xs)
    model = LogisticRegression(max_iter=800, class_weight="balanced")
    scores = cross_val_score(model, X, ys, cv=min(5, len(set(ys))), scoring="balanced_accuracy")
    model.fit(X, ys)
    MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(model, MODEL_PATH)
    meta = {
        "status": "trained",
        "samples": len(xs),
        "classes": sorted(set(ys)),
        "cv_balanced_accuracy": round(float(scores.mean()), 4),
        "path": str(MODEL_PATH),
    }
    MODEL_PATH.with_suffix(".json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return meta


if __name__ == "__main__":
    import argparse
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["train"])
    args = parser.parse_args()
    if args.command == "train":
        print(json.dumps(train_from_manifest(), indent=2))
