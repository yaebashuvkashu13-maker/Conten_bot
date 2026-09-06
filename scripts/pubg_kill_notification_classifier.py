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
        return np.zeros(14, dtype=np.float32)
    h, w = crop.shape[:2]
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    blue = cv2.inRange(hsv, np.array([90, 40, 60]), np.array([135, 255, 255]))
    cyan = cv2.inRange(hsv, np.array([78, 40, 80]), np.array([105, 255, 255]))
    gold = cv2.inRange(hsv, np.array([12, 80, 140]), np.array([38, 255, 255]))
    purple = cv2.inRange(hsv, np.array([125, 40, 70]), np.array([165, 255, 255]))
    red_a = cv2.inRange(hsv, np.array([0, 70, 90]), np.array([12, 255, 255]))
    red_b = cv2.inRange(hsv, np.array([168, 70, 90]), np.array([179, 255, 255]))
    warm = cv2.bitwise_or(cv2.bitwise_or(gold, purple), cv2.bitwise_or(red_a, red_b))
    cool = cv2.bitwise_or(blue, cyan)
    colored = cv2.bitwise_or(cool, warm)
    colored_ratio = float(np.count_nonzero(colored)) / max(colored.size, 1)
    warm_ratio = float(np.count_nonzero(warm)) / max(colored.size, 1)
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
            warm_ratio,
            float(np.count_nonzero(purple)) / max(colored.size, 1),
        ],
        dtype=np.float32,
    )


def heuristic_predict(crop: np.ndarray) -> tuple[str, float]:
    feat = _features(crop)
    colored, aspect, edge = float(feat[0]), float(feat[1]), float(feat[2])
    warm = float(feat[12]) if feat.size > 12 else 0.0
    purple = float(feat[13]) if feat.size > 13 else 0.0
    if colored < 0.02 and warm < 0.015:
        return "hud_fp", 0.15
    if aspect < 2.5 and warm < 0.04:
        return "map_blue", 0.25
    # Mobile Metro: purple kill-skin banners + red УБИЙСТВО text.
    if (warm >= 0.05 or purple >= 0.04) and aspect >= 2.4 and edge > 0.03:
        return "kill", min(0.94, 0.50 + warm * 2.2 + purple * 1.5 + edge)
    if colored > 0.08 and aspect >= 3.0 and edge > 0.04:
        return "kill", min(0.92, 0.45 + colored * 2.0 + edge)
    if colored > 0.06 and aspect >= 2.8:
        return "knock", min(0.85, 0.38 + colored * 1.6 + edge * 0.5)
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
