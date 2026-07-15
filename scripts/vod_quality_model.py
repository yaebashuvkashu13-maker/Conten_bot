#!/usr/bin/env python3
"""Per-game quality ranker for PUBG, Standoff, Genshin and WoT."""

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
from shooter_vod_segment_store import _paths

GAMES = ("pubg", "standoff", "genshin", "wot")
FEATURE_NAMES = (
    "score",
    "hook_score",
    "clip_score",
    "fight_dur",
    "duration",
    "panns_gunshot",
    "panns_machine_gun",
    "panns_explosion",
    "center_motion",
    "boss_bar",
    "hit_flash",
    "visual_pass",
)


def model_path(game: str) -> Path:
    game = game.strip().lower()
    return Path(
        os.environ.get(
            f"VOD_{game.upper()}_QUALITY_MODEL_PATH",
            str(_paths(game)["state"].parent / "vod_quality_classifier.joblib"),
        )
    )


def _read(path: Path, default: dict) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else default
    except (OSError, json.JSONDecodeError):
        return default


def quality_features(row: dict) -> list[float]:
    metrics = row.get("presend_metrics") or row.get("highlight_metrics") or {}

    def value(name: str, default: float = 0.0) -> float:
        raw = row.get(name)
        if raw in (None, ""):
            raw = metrics.get(name, default)
        try:
            return float(raw or default)
        except (TypeError, ValueError):
            return default

    hit_flash = value("hit_flash", value("hit_flash_count"))
    visual = row.get("visual_pass", metrics.get("visual_pass", True))
    return [
        value("score"),
        value("hook_score"),
        value("clip_score", value("score")),
        value("fight_dur", value("duration")),
        value("duration", value("fight_dur")),
        value("panns_gunshot"),
        value("panns_machine_gun"),
        value("panns_explosion"),
        value("center_motion"),
        value("boss_bar"),
        hit_flash,
        1.0 if visual else 0.0,
    ]


def load_training_rows(game: str) -> list[dict]:
    game = game.strip().lower()
    paths = _paths(game)
    labels = _read(paths["labels"], {"good": [], "bad": []})
    index = _read(paths["index"], {"segments": []})
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
            group = str(merged.get("vod_id") or "")
            if not group and "_" in sid:
                group = sid.rsplit("_", 1)[0]
            rows.append({**merged, "label": label, "group": group or sid})
    return rows


def training_ready(game: str) -> bool:
    rows = load_training_rows(game)
    good = sum(int(row["label"]) == 1 for row in rows)
    bad = len(rows) - good
    return len(rows) >= 30 and good >= 8 and bad >= 8


def _select_threshold(model, X: np.ndarray, y: np.ndarray, game: str) -> float:
    best: tuple[float, float] | None = None
    prefix = f"VOD_{game.upper()}_TRAIN"
    for threshold in np.arange(0.30, 0.951, 0.025):
        metrics = evaluate_binary(model, X, y, threshold=float(threshold))
        passed, _ = passes_quality_gate(metrics, env_prefix=prefix)
        if passed:
            candidate = (float(metrics["recall"]), float(threshold))
            if best is None or candidate > best:
                best = candidate
    return best[1] if best else 0.5


def train_and_promote(game: str) -> tuple[bool, dict]:
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler

    game = game.strip().lower()
    rows = load_training_rows(game)
    if not training_ready(game):
        return False, {"game": game, "reason": "insufficient_balanced_samples", "samples": len(rows)}
    X = np.asarray([quality_features(row) for row in rows], dtype=float)
    y = np.asarray([int(row["label"]) for row in rows], dtype=int)
    groups = [str(row.get("group") or "") for row in rows]
    try:
        train_idx, test_idx = grouped_holdout_indices(y, groups)
    except ValueError as exc:
        return False, {"game": game, "reason": str(exc), "samples": len(rows)}

    def new_model():
        return make_pipeline(
            StandardScaler(),
            LogisticRegression(max_iter=500, class_weight="balanced"),
        )

    validation = new_model()
    validation.fit(X[train_idx], y[train_idx])
    threshold = _select_threshold(validation, X[train_idx], y[train_idx], game)
    metrics = evaluate_binary(validation, X[test_idx], y[test_idx], threshold=threshold)
    passed, failures = passes_quality_gate(
        metrics,
        env_prefix=f"VOD_{game.upper()}_TRAIN",
    )
    final = new_model()
    final.fit(X, y)
    metadata = {
        "schema_version": 1,
        "game": game,
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
    candidate, _ = write_candidate(final, model_path(game), metadata)
    if not passed:
        return False, metadata
    promote_candidate(model_path(game), candidate, metadata)
    clear_model_cache(game)
    return True, metadata


@lru_cache(maxsize=8)
def _load_model(game: str):
    path = model_path(game)
    if not path.exists():
        return None, {}
    try:
        import joblib

        return joblib.load(path), _read(path.with_suffix(path.suffix + ".json"), {})
    except Exception:
        return None, {}


def clear_model_cache(game: str | None = None) -> None:
    _load_model.cache_clear()


def quality_gate(game: str, row: dict) -> tuple[bool, str, float]:
    game = game.strip().lower()
    if os.environ.get(f"VOD_{game.upper()}_QUALITY_MODEL", "1") != "1":
        return True, "quality_model_disabled", 0.0
    model, metadata = _load_model(game)
    if model is None:
        raw_required = os.environ.get(f"VOD_{game.upper()}_QUALITY_MODEL_REQUIRED")
        required = training_ready(game) if raw_required is None else raw_required == "1"
        return (not required), "quality_model_missing", 0.0
    probability = float(model.predict_proba(np.asarray([quality_features(row)]))[:, 1][0])
    threshold = float(
        os.environ.get(
            f"VOD_{game.upper()}_QUALITY_MODEL_THRESHOLD",
            str(metadata.get("threshold", 0.5)),
        )
    )
    if probability < threshold:
        return False, f"quality_model_low={probability:.3f}<{threshold:.3f}", probability
    return True, f"quality_model_pass={probability:.3f}>={threshold:.3f}", probability


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--game", choices=(*GAMES, "all"), required=True)
    parser.add_argument("--train", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    games = GAMES if args.game == "all" else (args.game,)
    reports: dict[str, dict] = {}
    all_pass = True
    for game in games:
        passed, report = train_and_promote(game)
        reports[game] = {"passed": passed, **report}
        if training_ready(game) and not passed:
            all_pass = False
    if args.json:
        print(json.dumps(reports, ensure_ascii=False, indent=2))
    else:
        print(reports)
    return 0 if all_pass else 2


if __name__ == "__main__":
    raise SystemExit(main())
