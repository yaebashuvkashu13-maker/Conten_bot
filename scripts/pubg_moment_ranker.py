#!/usr/bin/env python3
"""Train and apply a PUBG moment ranker from owner timestamps and part feedback."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

FEATURE_VERSION = 2
FEATURE_NAMES = (
    "panns_gunshot",
    "panns_machine_gun",
    "panns_explosion",
    "panns_speech",
    "panns_music",
    "panns_gun_max",
    "gunfire_density",
    "burst_ratio",
    "audio_rms",
)
_MODEL_CACHE: tuple[str, int, dict] | None = None


def _repo_root() -> Path:
    return Path(os.environ.get("CONTENT_BOT_REPO", Path(__file__).resolve().parent.parent))


def model_path() -> Path:
    return Path(
        os.environ.get(
            "PUBG_RANKER_MODEL",
            "/root/data/pubg/pubg_moment_ranker.joblib",
        )
    )


def feature_cache_root() -> Path:
    return Path(
        os.environ.get(
            "PUBG_RANKER_FEATURE_CACHE",
            "/root/data/pubg/ranker_features",
        )
    )


def owner_labels_path() -> Path:
    override = os.environ.get("PUBG_OWNER_LABELS_PATH", "").strip()
    if override:
        return Path(override)
    for path in (
        _repo_root() / "data" / "pubg_owner_labels.json",
        Path("/root/data/mlbb/pubg_owner_labels.json"),
    ):
        if path.exists():
            return path
    return _repo_root() / "data" / "pubg_owner_labels.json"


def feedback_path() -> Path:
    return Path(
        os.environ.get(
            "PUBG_SEGMENT_LABELS_PATH",
            "/root/data/pubg/vod_segment_labels.json",
        )
    )


def _read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def resolve_vod(video_id: str, *, hinted_path: str = "") -> Path | None:
    hinted = Path(hinted_path) if hinted_path else None
    candidates: list[Path] = []
    if hinted is not None:
        candidates.append(hinted)
    candidates.extend(
        [
            Path("/root/data/pubg/youtube_nightly/inbox") / f"yt_{video_id}.mp4",
            Path("/root/data/pubg/youtube_nightly/parked") / f"yt_{video_id}.mp4",
            Path("/root/data/pubg/youtube_nightly/park_timeout") / f"yt_{video_id}.mp4",
            Path("/root/data/pubg/regression_vods") / f"yt_{video_id}.mp4",
            _repo_root() / "data" / "samples" / f"yt_{video_id}.mp4",
        ]
    )
    for path in candidates:
        if path.is_file():
            return path
    return None


def _video_id(path: Path) -> str:
    stem = path.stem
    return stem[3:14] if stem.startswith("yt_") else stem[:11]


def _feature_key(video_path: Path, start_sec: float, duration_sec: float) -> str:
    stat = video_path.stat()
    raw = (
        f"v{FEATURE_VERSION}|{video_path.resolve()}|{stat.st_size}|{stat.st_mtime_ns}|"
        f"{start_sec:.2f}|{duration_sec:.2f}"
    )
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def extract_features(video_path: Path, start_sec: float, duration_sec: float = 14.0) -> dict[str, float]:
    """Extract inference-safe features; cache makes train/re-rank repeatable."""
    key = _feature_key(video_path, start_sec, duration_sec)
    cache_file = feature_cache_root() / f"{key}.json"
    cached = _read_json(cache_file)
    if cached.get("version") == FEATURE_VERSION and isinstance(cached.get("features"), dict):
        return {name: float(cached["features"].get(name, 0.0)) for name in FEATURE_NAMES}

    from highlight_scorer import score_panns_audio
    from gameplay_gate import score_pubg_gunfire_audio

    panns = score_panns_audio(video_path, start_sec, duration_sec)
    gun, burst, rms = score_pubg_gunfire_audio(video_path, start_sec, duration_sec)
    features = {
        "panns_gunshot": float(panns.get("panns_gunshot", 0.0)),
        "panns_machine_gun": float(panns.get("panns_machine_gun", 0.0)),
        "panns_explosion": float(panns.get("panns_explosion", 0.0)),
        "panns_speech": float(panns.get("panns_speech", 0.0)),
        "panns_music": float(panns.get("panns_music", 0.0)),
        "panns_gun_max": float(panns.get("panns_gun_max", 0.0)),
        "gunfire_density": float(gun),
        "burst_ratio": float(burst),
        "audio_rms": float(rms),
    }
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    payload = {"version": FEATURE_VERSION, "saved_at": time.time(), "features": features}
    tmp = cache_file.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, cache_file)
    return features


def feature_vector(features: dict[str, float]) -> list[float]:
    return [float(features.get(name, 0.0)) for name in FEATURE_NAMES]


@dataclass(frozen=True)
class TrainingSample:
    video_id: str
    video_path: Path
    peak_sec: float
    label: int
    source: str
    weight: float
    features: dict[str, float] | None = None


def features_from_quality_report(report: dict) -> dict[str, float] | None:
    if not isinstance(report, dict):
        return None
    features = {
        name: float(report.get(name, 0.0) or 0.0)
        for name in FEATURE_NAMES
    }
    if not any(features.values()):
        return None
    return features


def load_training_samples() -> list[TrainingSample]:
    """Owner labels first; newer per-part feedback overrides the same moment."""
    samples: dict[tuple[str, int], TrainingSample] = {}
    owner = _read_json(owner_labels_path())
    for video_id, rows in (owner.get("videos") or {}).items():
        vod = resolve_vod(str(video_id))
        if not vod or not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict) or "time_sec" not in row:
                continue
            label = 1 if row.get("label") == "good" else 0
            peak = float(row["time_sec"])
            samples[(str(video_id), round(peak))] = TrainingSample(
                str(video_id), vod, peak, label, "owner_label", 2.0
            )

    feedback = _read_json(feedback_path())
    for bucket, label in (("good", 1), ("bad", 0)):
        for row in feedback.get(bucket) or []:
            if not isinstance(row, dict):
                continue
            sid = str(row.get("segment_id") or "")
            video_id = str(row.get("vod_id") or "")
            if not video_id and "_" in sid:
                video_id = sid.rsplit("_", 1)[0]
            hinted = str(row.get("vod") or "")
            vod = resolve_vod(video_id, hinted_path=hinted)
            if not video_id or not vod:
                continue
            peak = float(row.get("peak_start", row.get("start", 0)) or 0)
            samples[(video_id, round(peak))] = TrainingSample(
                video_id,
                vod,
                peak,
                label,
                "part_feedback",
                1.0,
                features_from_quality_report(row.get("quality_metrics") or {}),
            )
    return sorted(samples.values(), key=lambda row: (row.video_id, row.peak_sec))


def training_signature() -> str:
    digest = hashlib.sha256()
    for path in (owner_labels_path(), feedback_path()):
        digest.update(str(path).encode())
        try:
            digest.update(path.read_bytes())
        except OSError:
            pass
    digest.update(str(FEATURE_VERSION).encode())
    return digest.hexdigest()


def _threshold_metrics(
    y_true: list[int],
    probabilities: list[float],
    threshold: float,
) -> tuple[float, float, float]:
    pred = [int(p >= threshold) for p in probabilities]
    tp = sum(1 for y, p in zip(y_true, pred) if y == p == 1)
    fp = sum(1 for y, p in zip(y_true, pred) if y == 0 and p == 1)
    tn = sum(1 for y, p in zip(y_true, pred) if y == p == 0)
    fn = sum(1 for y, p in zip(y_true, pred) if y == 1 and p == 0)
    precision = tp / max(tp + fp, 1)
    recall = tp / max(tp + fn, 1)
    specificity = tn / max(tn + fp, 1)
    balanced = (recall + specificity) * 0.5
    f1 = 2 * precision * recall / max(precision + recall, 1e-9)
    return balanced * 0.7 + f1 * 0.3, balanced, f1


def _best_threshold(y_true: list[int], probabilities: list[float]) -> tuple[float, float, float]:
    if not y_true or len(set(y_true)) < 2:
        return 0.5, 0.0, 0.0
    best = (0.0, 0.5, 0.0, 0.0)
    for threshold in [x / 100 for x in range(30, 76, 5)]:
        objective, balanced, f1 = _threshold_metrics(y_true, probabilities, threshold)
        if (objective, threshold) > (best[0], best[1]):
            best = (objective, threshold, balanced, f1)
    return best[1], best[2], best[3]


def train(*, if_changed: bool = False) -> dict[str, Any]:
    global _MODEL_CACHE
    import joblib
    import numpy as np
    from sklearn.ensemble import RandomForestClassifier
    from sklearn.linear_model import LogisticRegression
    from sklearn.model_selection import LeaveOneGroupOut
    from sklearn.pipeline import Pipeline
    from sklearn.preprocessing import StandardScaler

    output = model_path()
    samples = load_training_samples()
    sample_state = [
        (
            row.video_id,
            round(row.peak_sec, 2),
            row.label,
            row.source,
            row.video_path.stat().st_size,
            row.video_path.stat().st_mtime_ns,
        )
        for row in samples
    ]
    signature = hashlib.sha256(
        f"{training_signature()}|{sample_state}".encode()
    ).hexdigest()
    if if_changed and output.exists():
        try:
            old = joblib.load(output)
            if old.get("training_signature") == signature:
                return {"status": "unchanged", "path": str(output)}
        except Exception:
            pass

    positives = sum(row.label for row in samples)
    negatives = len(samples) - positives
    if len(samples) < 12 or positives < 4 or negatives < 4:
        return {
            "status": "insufficient_samples",
            "samples": len(samples),
            "positive": positives,
            "negative": negatives,
        }

    X = np.asarray(
        [
            feature_vector(
                row.features
                or extract_features(row.video_path, max(0.0, row.peak_sec - 7.0), 14.0)
            )
            for row in samples
        ],
        dtype=np.float32,
    )
    y = np.asarray([row.label for row in samples], dtype=np.int32)
    groups = np.asarray([row.video_id for row in samples])
    weights = np.asarray([row.weight for row in samples], dtype=np.float32)

    factories = {
        "logistic_regression": lambda: Pipeline(
            [
                ("scale", StandardScaler()),
                ("model", LogisticRegression(max_iter=1000, class_weight="balanced")),
            ]
        ),
        "random_forest": lambda: RandomForestClassifier(
            n_estimators=300,
            max_depth=5,
            min_samples_leaf=2,
            class_weight="balanced",
            random_state=42,
            n_jobs=1,
        ),
    }

    def fit_model(model: Any, name: str, x: Any, target: Any, sample_weight: Any) -> None:
        if name == "logistic_regression":
            model.fit(x, target, model__sample_weight=sample_weight)
        else:
            model.fit(x, target, sample_weight=sample_weight)

    evaluations: dict[str, dict[str, Any]] = {}
    if len(set(groups)) >= 3:
        for name, factory in factories.items():
            oof_y: list[int] = []
            oof_p: list[float] = []
            for train_idx, test_idx in LeaveOneGroupOut().split(X, y, groups):
                if len(set(y[train_idx])) < 2:
                    continue
                fold = factory()
                fit_model(fold, name, X[train_idx], y[train_idx], weights[train_idx])
                oof_y.extend(int(v) for v in y[test_idx])
                oof_p.extend(float(v) for v in fold.predict_proba(X[test_idx])[:, 1])
            threshold, balanced, f1 = _best_threshold(oof_y, oof_p)
            evaluations[name] = {
                "threshold": threshold,
                "balanced_accuracy": balanced,
                "f1": f1,
                "samples": len(oof_y),
                "objective": balanced * 0.7 + f1 * 0.3,
            }
    model_name = max(
        evaluations,
        key=lambda name: float(evaluations[name]["objective"]),
        default="logistic_regression",
    )
    selected = evaluations.get(
        model_name,
        {"threshold": 0.5, "balanced_accuracy": None, "f1": None},
    )
    threshold = float(selected["threshold"])
    oof_balanced_accuracy = selected["balanced_accuracy"]
    model = factories[model_name]()
    fit_model(model, model_name, X, y, weights)
    artifact = {
        "artifact_version": 1,
        "feature_version": FEATURE_VERSION,
        "feature_names": FEATURE_NAMES,
        "model": model,
        "model_name": model_name,
        "threshold": threshold,
        "training_signature": signature,
        "trained_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "samples": len(samples),
        "positive": positives,
        "negative": negatives,
        "groups": len(set(groups)),
        "oof_balanced_accuracy": oof_balanced_accuracy,
        "oof_f1": selected["f1"],
        "candidate_models": evaluations,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=output.parent, suffix=".joblib", delete=False) as handle:
        tmp_path = Path(handle.name)
    try:
        joblib.dump(artifact, tmp_path)
        os.replace(tmp_path, output)
        _MODEL_CACHE = None
    finally:
        tmp_path.unlink(missing_ok=True)
    report = {key: value for key, value in artifact.items() if key != "model"} | {
        "status": "trained",
        "path": str(output),
    }
    report_path = output.with_suffix(".json")
    report_path.write_text(
        json.dumps(report, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return report


def _load_artifact() -> dict | None:
    global _MODEL_CACHE
    path = model_path()
    if not path.is_file():
        return None
    try:
        mtime_ns = path.stat().st_mtime_ns
    except OSError:
        return None
    if _MODEL_CACHE and _MODEL_CACHE[:2] == (str(path), mtime_ns):
        return _MODEL_CACHE[2]
    try:
        import joblib

        artifact = joblib.load(path)
    except Exception:
        return None
    if tuple(artifact.get("feature_names") or ()) != FEATURE_NAMES:
        return None
    validation = artifact.get("oof_balanced_accuracy")
    min_validation = float(os.environ.get("PUBG_RANKER_MIN_OOF_BALANCED_ACCURACY", "0.52"))
    if validation is None and os.environ.get("PUBG_RANKER_ALLOW_UNVALIDATED", "0") != "1":
        return None
    if validation is not None and float(validation) < min_validation:
        return None
    _MODEL_CACHE = (str(path), mtime_ns, artifact)
    return artifact


def predict_score(video_path: Path, start_sec: float, duration_sec: float = 14.0) -> float | None:
    artifact = _load_artifact()
    if artifact is None:
        return None
    features = extract_features(video_path, start_sec, duration_sec)
    probability = artifact["model"].predict_proba([feature_vector(features)])[0][1]
    return float(probability)


def predict_from_features(features: dict[str, float]) -> float | None:
    artifact = _load_artifact()
    if artifact is None:
        return None
    probability = artifact["model"].predict_proba([feature_vector(features)])[0][1]
    return float(probability)


def ranker_available() -> bool:
    return _load_artifact() is not None


def rank_peaks_with_model(
    video_path: Path,
    peaks: list[float],
    *,
    part_sec: float = 14.0,
    max_probes: int | None = None,
) -> tuple[list[float], str]:
    artifact = _load_artifact()
    if artifact is None or os.environ.get("PUBG_RANKER_ENABLED", "1") != "1":
        return list(peaks), "ranker_unavailable"
    cap = max_probes or int(os.environ.get("PUBG_RANKER_MAX_PROBES", "16"))
    cap = min(len(peaks), max(1, cap))
    selected_indices: list[int] = list(range(min(len(peaks), max(1, cap // 2))))
    selected_set = set(selected_indices)
    # Reserve half the budget for timeline diversity. Global audio rank alone
    # repeatedly omitted quieter fights from later VOD chapters.
    seen_chunks: set[int] = set()
    for index in sorted(range(len(peaks)), key=lambda idx: float(peaks[idx])):
        chunk = int(float(peaks[index]) // 300.0)
        if chunk in seen_chunks or index in selected_set:
            continue
        seen_chunks.add(chunk)
        selected_indices.append(index)
        selected_set.add(index)
        if len(selected_indices) >= cap:
            break
    for index in range(len(peaks)):
        if len(selected_indices) >= cap:
            break
        if index not in selected_set:
            selected_indices.append(index)
            selected_set.add(index)

    def extract(index: int) -> tuple[int, float, dict[str, float] | None]:
        peak = float(peaks[index])
        start = max(0.0, peak - part_sec * 0.5)
        try:
            features = extract_features(video_path, start, part_sec)
        except Exception:
            features = None
        return index, peak, features

    workers = max(1, int(os.environ.get("PUBG_RANKER_WORKERS", "4")))
    if workers == 1 or len(selected_indices) < 2:
        extracted = [extract(index) for index in selected_indices]
    else:
        with ThreadPoolExecutor(max_workers=min(workers, len(selected_indices))) as pool:
            extracted = list(pool.map(extract, selected_indices))
    valid = [(index, peak, features) for index, peak, features in extracted if features is not None]
    probabilities = (
        artifact["model"].predict_proba(
            [feature_vector(features) for _index, _peak, features in valid]
        )[:, 1]
        if valid
        else []
    )
    probability_by_index = {
        index: float(probability)
        for (index, _peak, _features), probability in zip(valid, probabilities)
    }
    scored = [
        (probability_by_index.get(index, -1.0), index, peak)
        for index, peak, _features in extracted
    ]
    scored.sort(key=lambda row: (-row[0], row[1]))
    ranked = [peak for _score, _index, peak in scored]
    ranked.extend(float(peak) for index, peak in enumerate(peaks) if index not in selected_set)
    top = scored[0][0] if scored else -1.0
    return ranked, f"ranker top={top:.3f} n={len(scored)}"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--train", action="store_true")
    parser.add_argument("--train-if-changed", action="store_true")
    args = parser.parse_args()
    if not args.train and not args.train_if_changed:
        parser.error("choose --train or --train-if-changed")
    report = train(if_changed=args.train_if_changed)
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if report["status"] in {"trained", "unchanged"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
