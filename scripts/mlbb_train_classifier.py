#!/usr/bin/env python3
"""Train highlight_classifier.joblib from owner Shorts labels + exemplars."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


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


def _classifier_out() -> Path:
    repo = _repo_root()
    return Path(
        os.environ.get(
            "HIGHLIGHT_CLASSIFIER_PATH",
            str(repo / "data" / "mlbb" / "highlight_classifier_mobile_legends.joblib"),
        )
    )


def load_training_rows() -> tuple[list[list[float]], list[int]]:
    path = _labels_path()
    if not path.exists():
        return [], []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return [], []
    xs: list[list[float]] = []
    ys: list[int] = []
    for section, label in (("good", 1), ("bad", 0)):
        for row in data.get(section, []):
            score = float(row.get("score") or row.get("model_score") or 0)
            xs.append([score, float(row.get("clip_start_sec") or 0)])
            ys.append(label)
    for row in data.get("feedback", []):
        owner = row.get("owner_label")
        if owner not in ("yes", "good", "no", "bad"):
            continue
        score = float(row.get("model_score") or row.get("score") or 0)
        xs.append([score, 0.0])
        ys.append(1 if owner in ("yes", "good") else 0)
    return xs, ys


def train_and_save() -> tuple[bool, str]:
    xs, ys = load_training_rows()
    min_rows = int(os.environ.get("MLBB_TRAIN_MIN_ROWS", "20"))
    if len(xs) < min_rows:
        return False, f"too_few_rows={len(xs)}<{min_rows}"
    try:
        import numpy as np
        from sklearn.linear_model import LogisticRegression
        import joblib
    except ImportError as exc:
        return False, f"missing_dep={exc}"

    x = np.array(xs, dtype=np.float64)
    y = np.array(ys, dtype=np.int32)
    if len(set(y.tolist())) < 2:
        return False, "single_class"

    clf = LogisticRegression(max_iter=500, class_weight="balanced")
    clf.fit(x, y)
    out = _classifier_out()
    out.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(clf, out)
    acc = float(clf.score(x, y))
    return True, f"saved={out} rows={len(xs)} train_acc={acc:.3f}"


def main() -> int:
    ok, msg = train_and_save()
    print(msg, flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
