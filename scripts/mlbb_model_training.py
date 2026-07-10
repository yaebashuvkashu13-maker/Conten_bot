#!/usr/bin/env python3
"""CPU-friendly training helpers with VOD-grouped evaluation and atomic promotion."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
from pathlib import Path
from typing import Sequence

import numpy as np

from vod_state_io import load_json_state, save_json_state


FEATURE_SCHEMA_VERSION = 1


def feature_cache_path() -> Path:
    root = Path(os.environ.get("MLBB_DATA_ROOT", "/root/data/mlbb"))
    return Path(
        os.environ.get(
            "MLBB_TRAIN_FEATURE_CACHE",
            str(root / "highlight_training_features.json"),
        )
    )


def load_feature_cache() -> dict:
    return load_json_state(
        feature_cache_path(),
        {"schema_version": FEATURE_SCHEMA_VERSION, "features": {}},
    )


def save_feature_cache(cache: dict) -> None:
    cache["schema_version"] = FEATURE_SCHEMA_VERSION
    cache["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    save_json_state(feature_cache_path(), cache)


def feature_key(path: Path, start: float, profile: str) -> str:
    stat = path.stat()
    raw = "|".join(
        (
            str(path.resolve()),
            str(stat.st_size),
            str(stat.st_mtime_ns),
            f"{float(start):.3f}",
            profile,
            str(FEATURE_SCHEMA_VERSION),
        )
    )
    return hashlib.sha256(raw.encode()).hexdigest()


def grouped_holdout_indices(
    labels: Sequence[int],
    groups: Sequence[str],
    *,
    test_fraction: float = 0.25,
) -> tuple[np.ndarray, np.ndarray]:
    """Deterministic group split containing both classes on each side."""
    y = np.asarray(labels, dtype=int)
    g = np.asarray(groups, dtype=object)
    unique = sorted(set(str(x) for x in g))
    if len(unique) < 3 or len(set(y.tolist())) < 2:
        raise ValueError("need at least 3 groups and both classes")
    target = max(1, round(len(unique) * test_fraction))
    for salt in range(100):
        ranked = sorted(
            unique,
            key=lambda value: hashlib.sha256(f"{salt}:{value}".encode()).hexdigest(),
        )
        test_groups = set(ranked[:target])
        test = np.asarray([i for i, value in enumerate(g) if str(value) in test_groups])
        train = np.asarray([i for i, value in enumerate(g) if str(value) not in test_groups])
        if len(train) and len(test) and len(set(y[train])) == 2 and len(set(y[test])) == 2:
            return train, test
    raise ValueError("unable to build class-balanced VOD-grouped holdout")


def evaluate_binary(
    model,
    X: np.ndarray,
    y: np.ndarray,
    *,
    threshold: float = 0.5,
) -> dict:
    probabilities = np.asarray(model.predict_proba(X))[:, 1]
    predicted = (probabilities >= threshold).astype(int)
    tp = int(np.sum((predicted == 1) & (y == 1)))
    fp = int(np.sum((predicted == 1) & (y == 0)))
    tn = int(np.sum((predicted == 0) & (y == 0)))
    fn = int(np.sum((predicted == 0) & (y == 1)))
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    bad_false_pass = fp / (fp + tn) if fp + tn else 0.0
    return {
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "bad_false_pass": round(bad_false_pass, 4),
        "tp": tp,
        "fp": fp,
        "tn": tn,
        "fn": fn,
        "evaluated": int(len(y)),
        "threshold": threshold,
    }


def passes_quality_gate(metrics: dict) -> tuple[bool, list[str]]:
    min_precision = float(os.environ.get("MLBB_TRAIN_MIN_PRECISION", "0.85"))
    min_recall = float(os.environ.get("MLBB_TRAIN_MIN_RECALL", "0.70"))
    max_bad_fp = float(os.environ.get("MLBB_TRAIN_MAX_BAD_FALSE_PASS", "0.10"))
    failures: list[str] = []
    if float(metrics.get("precision") or 0) < min_precision:
        failures.append(f"precision<{min_precision:.2f}")
    if float(metrics.get("recall") or 0) < min_recall:
        failures.append(f"recall<{min_recall:.2f}")
    if float(metrics.get("bad_false_pass") or 1) > max_bad_fp:
        failures.append(f"bad_false_pass>{max_bad_fp:.2f}")
    return not failures, failures


def write_candidate(model, output_path: Path, metadata: dict) -> tuple[Path, Path]:
    import joblib

    output_path.parent.mkdir(parents=True, exist_ok=True)
    candidate = output_path.with_suffix(output_path.suffix + ".candidate")
    candidate_meta = output_path.with_suffix(output_path.suffix + ".candidate.json")
    tmp = candidate.with_suffix(candidate.suffix + f".tmp.{os.getpid()}")
    joblib.dump(model, tmp)
    os.replace(tmp, candidate)
    save_json_state(candidate_meta, metadata)
    return candidate, candidate_meta


def promote_candidate(output_path: Path, candidate: Path, metadata: dict) -> Path:
    """Back up current model, then atomically promote a passed candidate."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.exists():
        backup_dir = output_path.parent / "model_backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%dT%H%M%SZ")
        shutil.copy2(output_path, backup_dir / f"{output_path.stem}_{stamp}{output_path.suffix}")
    os.replace(candidate, output_path)
    metadata_path = output_path.with_suffix(output_path.suffix + ".json")
    save_json_state(metadata_path, metadata)
    return output_path
