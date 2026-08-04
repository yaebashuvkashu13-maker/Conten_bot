#!/usr/bin/env python3
"""Train the MLBB visual announcement classifier from labeled 160×48 crops."""

from __future__ import annotations

import argparse
import json
import re
import time
from collections import Counter
from pathlib import Path

import numpy as np

from mlbb_event_classifier import (
    COMMAND,
    ENEMY_STREAK,
    FEATURE_VERSION,
    OTHER,
    OWN_STREAK,
    extract_visual_features,
)


def _video_group(path: Path) -> str:
    stem = path.stem
    match = re.match(r"(.+)_\d+(?:\.\d+)?$", stem)
    return match.group(1) if match else stem


def discover_samples(root: Path) -> tuple[list[Path], list[str], list[str], dict[Path, int]]:
    paths: list[Path] = []
    labels: list[str] = []
    groups: list[str] = []
    tier_labels: dict[Path, int] = {}

    def add(pattern: str, label: str, *, tier: int = 0) -> None:
        for path in sorted(root.glob(pattern)):
            if not path.is_file():
                continue
            paths.append(path)
            labels.append(label)
            groups.append(_video_group(path))
            if tier:
                tier_labels[path] = tier

    add("owner_cal/positive/**/*.png", OWN_STREAK)
    add("vod_crops/double/*.png", OWN_STREAK, tier=2)
    add("vod_crops/triple/*.png", OWN_STREAK, tier=3)
    add("vod_crops/savage/*.png", OWN_STREAK, tier=5)
    add("owner_cal/negative/enemy_kill/*.png", ENEMY_STREAK)
    add("owner_cal/negative/coordination/*.png", COMMAND)
    for folder in ("no_banner", "not_kill", "wrong_hero", "not_gameplay"):
        add(f"owner_cal/negative/{folder}/*.png", OTHER)
    add("vod_crops/unknown/*.png", OTHER)
    return paths, labels, groups, tier_labels


def _load_features(paths: list[Path]) -> tuple[np.ndarray, list[Path]]:
    import cv2

    rows: list[np.ndarray] = []
    kept: list[Path] = []
    for path in paths:
        image = cv2.imread(str(path))
        if image is None:
            continue
        try:
            rows.append(extract_visual_features(image))
            kept.append(path)
        except (ValueError, cv2.error):
            continue
    if not rows:
        raise RuntimeError("no readable training images")
    return np.vstack(rows), kept


def _pipeline():
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler

    return make_pipeline(
        StandardScaler(with_mean=False),
        LogisticRegression(
            C=0.7,
            class_weight="balanced",
            max_iter=2500,
            solver="lbfgs",
            random_state=17,
        ),
    )


def _group_split(labels: list[str], groups: list[str]) -> tuple[np.ndarray, np.ndarray]:
    from sklearn.model_selection import GroupShuffleSplit

    all_labels = set(labels)
    indices = np.arange(len(labels))
    for seed in range(17, 97):
        splitter = GroupShuffleSplit(n_splits=1, test_size=0.25, random_state=seed)
        train, test = next(splitter.split(indices, labels, groups))
        if set(np.asarray(labels)[train]) == all_labels and set(np.asarray(labels)[test]) == all_labels:
            return train, test
    raise RuntimeError("could not create a group split containing every class")


def train(root: Path, output: Path, *, min_per_class: int = 5) -> dict:
    paths, labels, groups, tier_map = discover_samples(root)
    counts = Counter(labels)
    missing = {label: count for label, count in counts.items() if count < min_per_class}
    expected = {OWN_STREAK, ENEMY_STREAK, COMMAND, OTHER}
    if set(counts) != expected or missing:
        raise RuntimeError(f"insufficient classes counts={dict(counts)} minimum={min_per_class}")

    features, kept = _load_features(paths)
    keep_set = set(kept)
    filtered = [(path, label, group) for path, label, group in zip(paths, labels, groups) if path in keep_set]
    paths = [row[0] for row in filtered]
    labels = [row[1] for row in filtered]
    groups = [row[2] for row in filtered]

    train_idx, test_idx = _group_split(labels, groups)
    event_model = _pipeline()
    y = np.asarray(labels)
    event_model.fit(features[train_idx], y[train_idx])
    predictions = event_model.predict(features[test_idx])

    from sklearn.metrics import classification_report, confusion_matrix

    report = classification_report(
        y[test_idx],
        predictions,
        labels=sorted(expected),
        output_dict=True,
        zero_division=0,
    )
    own_precision = float(report[OWN_STREAK]["precision"])
    own_recall = float(report[OWN_STREAK]["recall"])

    tier_indices = [index for index, path in enumerate(paths) if path in tier_map]
    if len(tier_indices) < 15 or len({tier_map[paths[index]] for index in tier_indices}) < 3:
        raise RuntimeError("insufficient tier-labeled double/triple/savage samples")
    tier_model = _pipeline()
    tier_model.fit(features[tier_indices], np.asarray([str(tier_map[paths[index]]) for index in tier_indices]))

    # Persist models fitted on all available event samples after holdout metrics.
    event_model.fit(features, y)
    artifact = {
        "feature_version": FEATURE_VERSION,
        "trained_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "event_model": event_model,
        "tier_model": tier_model,
        "class_counts": dict(Counter(labels)),
        "tier_counts": dict(Counter(tier_map[path] for path in paths if path in tier_map)),
        "holdout": {
            "size": int(len(test_idx)),
            "own_precision": round(own_precision, 4),
            "own_recall": round(own_recall, 4),
            "report": report,
            "confusion_matrix": confusion_matrix(
                y[test_idx], predictions, labels=sorted(expected)
            ).tolist(),
            "labels": sorted(expected),
        },
    }
    if own_precision < 0.70 or own_recall < 0.55:
        raise RuntimeError(
            f"unsafe holdout own precision/recall: {own_precision:.3f}/{own_recall:.3f}"
        )
    import joblib

    output.parent.mkdir(parents=True, exist_ok=True)
    temp = output.with_suffix(output.suffix + ".tmp")
    joblib.dump(artifact, temp)
    temp.replace(output)
    return {
        "output": str(output),
        "samples": len(paths),
        "class_counts": artifact["class_counts"],
        "tier_counts": artifact["tier_counts"],
        "holdout": artifact["holdout"],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--root",
        type=Path,
        default=Path("data/mlbb_kill_banners"),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("data/mlbb_event_classifier.joblib"),
    )
    parser.add_argument("--min-per-class", type=int, default=5)
    args = parser.parse_args()
    print(json.dumps(train(args.root, args.output, min_per_class=args.min_per_class), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
