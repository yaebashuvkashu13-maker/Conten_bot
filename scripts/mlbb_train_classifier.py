#!/usr/bin/env python3
"""Train highlight_classifier.joblib from owner labels + Shorts index metrics."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mlbb_classifier_features import (  # noqa: E402
    MLBB_CLASSIFIER_FEATURE_NAMES,
    attach_classifier_metadata,
    mlbb_classifier_feature_vector,
)


def _repo_root() -> Path:
    env = os.environ.get("CONTENT_BOT_REPO", "").strip()
    if env:
        return Path(env)
    root = Path(__file__).resolve().parent.parent
    if root.name == "bin" or str(root) == "/usr/local":
        return Path("/root/content_bot_ml")
    return root


def _labels_path() -> Path:
    data = Path(os.environ.get("MLBB_DATA_ROOT", "/root/data/mlbb"))
    return Path(os.environ.get("MLBB_CALIBRATION_LABELS", str(data / "calibration_labels.json")))


def _index_path() -> Path:
    data = Path(os.environ.get("MLBB_DATA_ROOT", "/root/data/mlbb"))
    return Path(os.environ.get("MLBB_SHORTS_INDEX", str(data / "youtube_shorts_index.json")))


def _classifier_out() -> Path:
    repo = _repo_root()
    return Path(
        os.environ.get(
            "HIGHLIGHT_CLASSIFIER_PATH",
            str(repo / "data" / "mlbb" / "highlight_classifier_mobile_legends.joblib"),
        )
    )


def _index_lookup() -> dict[str, dict]:
    path = _index_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    out: dict[str, dict] = {}
    for row in data.get("videos", []):
        vid = str(row.get("id") or row.get("video_id") or "").strip()
        if vid:
            out[vid] = row
    return out


def _merge_metrics(row: dict, index: dict[str, dict]) -> dict:
    vid = str(row.get("video_id") or row.get("id") or "").strip()
    base = dict(index.get(vid, {}))
    base.update(row)
    if "clip_score" not in base and "score" in base:
        base.setdefault("clip_score", base.get("score", 0))
    return base


def load_training_rows() -> tuple[list[list[float]], list[int]]:
    path = _labels_path()
    if not path.exists():
        return [], []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return [], []
    index = _index_lookup()
    xs: list[list[float]] = []
    ys: list[int] = []
    for section, label in (("good", 1), ("bad", 0)):
        for row in data.get(section, []):
            merged = _merge_metrics(row, index)
            xs.append(mlbb_classifier_feature_vector(merged))
            ys.append(label)
    for row in data.get("feedback", []):
        owner = row.get("owner_label")
        if owner not in ("yes", "good", "no", "bad"):
            continue
        merged = _merge_metrics(row, index)
        xs.append(mlbb_classifier_feature_vector(merged))
        ys.append(1 if owner in ("yes", "good") else 0)
    return xs, ys


def train_and_save() -> tuple[bool, str]:
    xs, ys = load_training_rows()
    min_rows = int(os.environ.get("MLBB_TRAIN_MIN_ROWS", "20"))
    if len(xs) < min_rows:
        return False, f"too_few_rows={len(xs)}<{min_rows}"
    try:
        import joblib
        import numpy as np
        from sklearn.linear_model import LogisticRegression
    except ImportError as exc:
        return False, f"missing_dep={exc}"

    x = np.array(xs, dtype=np.float64)
    y = np.array(ys, dtype=np.int32)
    if len(set(y.tolist())) < 2:
        return False, "single_class"

    clf = LogisticRegression(max_iter=500, class_weight="balanced")
    clf.fit(x, y)
    attach_classifier_metadata(clf)
    out = _classifier_out()
    out.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(clf, out)
    acc = float(clf.score(x, y))
    return True, f"saved={out} rows={len(xs)} features={','.join(MLBB_CLASSIFIER_FEATURE_NAMES)} train_acc={acc:.3f}"


def main() -> int:
    ok, msg = train_and_save()
    print(msg, flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
