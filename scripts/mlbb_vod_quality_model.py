#!/usr/bin/env python3
"""Fast clip-quality ranker trained from historical owner-rated VOD rows."""

from __future__ import annotations

import argparse
import json
import os
import time
from functools import lru_cache
from pathlib import Path

import numpy as np

from mlbb_model_training import (
    evaluate_binary,
    grouped_holdout_indices,
    passes_quality_gate,
    promote_candidate,
    write_candidate,
)


FEATURE_NAMES = (
    "score",
    "hook_score",
    "clip_score",
    "fight_dur",
    "duration",
    "kill_banner_tier",
    "has_banner",
    "has_clip_score",
    "has_fight_dur",
)


def _data_root() -> Path:
    return Path(os.environ.get("MLBB_DATA_ROOT", "/root/data/mlbb"))


def model_path() -> Path:
    return Path(
        os.environ.get(
            "MLBB_VOD_QUALITY_MODEL_PATH",
            str(_data_root() / "vod_quality_classifier.joblib"),
        )
    )


def _read(path: Path, default: dict) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else default
    except (OSError, json.JSONDecodeError):
        return default


def quality_features(row: dict) -> list[float]:
    has_clip = row.get("clip_score") not in (None, "")
    has_fight = row.get("fight_dur") not in (None, "")
    score = float(row.get("score") or 0)
    return [
        score,
        float(row.get("hook_score") or 0),
        float(row.get("clip_score") if has_clip else score),
        float(row.get("fight_dur") or row.get("duration") or 0),
        float(row.get("duration") or row.get("fight_dur") or 0),
        float(row.get("kill_banner_tier") or 0),
        1.0 if row.get("kill_banner") else 0.0,
        1.0 if has_clip else 0.0,
        1.0 if has_fight else 0.0,
    ]


def load_training_rows() -> list[dict]:
    labels = _read(_data_root() / "vod_segment_labels.json", {"good": [], "bad": []})
    index = _read(_data_root() / "vod_segment_index.json", {"segments": []})
    by_id = {
        str(row.get("segment_id")): row
        for row in index.get("segments", [])
        if row.get("segment_id")
    }
    rows: list[dict] = []
    for bucket, label in (("good", 1), ("bad", 0)):
        for item in labels.get(bucket, []):
            sid = str(item.get("segment_id") or "")
            merged = {**by_id.get(sid, {}), **item}
            vid = str(merged.get("vod_id") or "")
            if not vid and "_" in sid:
                vid = sid.rsplit("_", 1)[0]
            if not vid:
                vod = Path(str(merged.get("vod") or "")).stem
                vid = vod[3:] if vod.startswith("yt_") else vod
            rows.append(
                {
                    **merged,
                    "label": label,
                    "group": vid or sid,
                }
            )
    return rows


def _select_threshold(model, X: np.ndarray, y: np.ndarray) -> float:
    best: tuple[float, float] | None = None
    for threshold in np.arange(0.30, 0.951, 0.025):
        metrics = evaluate_binary(model, X, y, threshold=float(threshold))
        passed, _ = passes_quality_gate(metrics)
        if not passed:
            continue
        candidate = (float(metrics["recall"]), float(threshold))
        if best is None or candidate > best:
            best = candidate
    return best[1] if best else 0.5


def train_and_promote() -> tuple[bool, dict]:
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler

    rows = load_training_rows()
    if len(rows) < 30 or len({int(row["label"]) for row in rows}) < 2:
        return False, {"reason": "insufficient_samples", "samples": len(rows)}
    X = np.asarray([quality_features(row) for row in rows], dtype=float)
    y = np.asarray([int(row["label"]) for row in rows], dtype=int)
    groups = [str(row.get("group") or "") for row in rows]
    try:
        train_idx, test_idx = grouped_holdout_indices(y, groups)
    except ValueError as exc:
        return False, {"reason": str(exc), "samples": len(rows)}

    def new_model():
        return make_pipeline(
            StandardScaler(),
            LogisticRegression(max_iter=500, class_weight="balanced"),
        )

    validation = new_model()
    validation.fit(X[train_idx], y[train_idx])
    threshold = _select_threshold(validation, X[train_idx], y[train_idx])
    metrics = evaluate_binary(validation, X[test_idx], y[test_idx], threshold=threshold)
    passed, failures = passes_quality_gate(metrics)
    final = new_model()
    final.fit(X, y)
    metadata = {
        "schema_version": 1,
        "trained_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "samples": len(rows),
        "groups": len(set(groups)),
        "good": int(np.sum(y == 1)),
        "bad": int(np.sum(y == 0)),
        "feature_names": list(FEATURE_NAMES),
        "threshold": threshold,
        "holdout": metrics,
        "quality_pass": passed,
        "failures": failures,
    }
    candidate, _ = write_candidate(final, model_path(), metadata)
    if not passed:
        return False, metadata
    promote_candidate(model_path(), candidate, metadata)
    clear_model_cache()
    return True, metadata


@lru_cache(maxsize=1)
def _load_model():
    path = model_path()
    if not path.exists():
        return None, {}
    try:
        import joblib

        model = joblib.load(path)
        metadata = _read(path.with_suffix(path.suffix + ".json"), {})
        return model, metadata
    except Exception:
        return None, {}


def clear_model_cache() -> None:
    _load_model.cache_clear()


def quality_probability(row: dict) -> tuple[float, float]:
    model, metadata = _load_model()
    if model is None:
        return 0.0, 1.0
    probability = float(model.predict_proba(np.asarray([quality_features(row)]))[:, 1][0])
    threshold = float(
        os.environ.get(
            "MLBB_VOD_QUALITY_MODEL_THRESHOLD",
            str(metadata.get("threshold", 0.5)),
        )
    )
    return probability, threshold


def quality_gate(row: dict) -> tuple[bool, str, float]:
    if os.environ.get("MLBB_VOD_QUALITY_MODEL", "1") != "1":
        return True, "quality_model_disabled", 0.0
    model, _metadata = _load_model()
    if model is None:
        required = os.environ.get("MLBB_VOD_QUALITY_MODEL_REQUIRED", "1") == "1"
        return (not required), "quality_model_missing", 0.0
    probability, threshold = quality_probability(row)
    if probability < threshold:
        return False, f"quality_model_low={probability:.3f}<{threshold:.3f}", probability
    return True, f"quality_model_pass={probability:.3f}>={threshold:.3f}", probability


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--train", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    if not args.train:
        parser.error("--train is required")
    passed, report = train_and_promote()
    if args.json:
        print(json.dumps({"passed": passed, **report}, ensure_ascii=False, indent=2))
    else:
        print(f"quality_model passed={passed} report={report}")
    return 0 if passed else 2


if __name__ == "__main__":
    raise SystemExit(main())
