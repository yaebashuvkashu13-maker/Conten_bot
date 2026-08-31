#!/usr/bin/env python3
"""Train and apply a PUBG moment ranker from owner timestamps and part feedback."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

FEATURE_VERSION = 1
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
    "center_motion",
    "killfeed_density",
)


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
    from pubg_killfeed_ocr import score_killfeed_segment
    from pubg_shooting_gate import pubg_probe_segment

    panns = score_panns_audio(video_path, start_sec, duration_sec)
    shoot = pubg_probe_segment(video_path, start_sec, duration_sec)
    try:
        killfeed, _ = score_killfeed_segment(video_path, start_sec, duration_sec, "pubg")
    except Exception:
        killfeed = 0.0
    features = {
        "panns_gunshot": float(panns.get("panns_gunshot", 0.0)),
        "panns_machine_gun": float(panns.get("panns_machine_gun", 0.0)),
        "panns_explosion": float(panns.get("panns_explosion", 0.0)),
        "panns_speech": float(panns.get("panns_speech", 0.0)),
        "panns_music": float(panns.get("panns_music", 0.0)),
        "panns_gun_max": float(panns.get("panns_gun_max", 0.0)),
        "gunfire_density": float(shoot.get("gunfire_density", 0.0)),
        "burst_ratio": float(shoot.get("burst_ratio", 0.0)),
        "audio_rms": float(shoot.get("audio_rms", 0.0)),
        "center_motion": float(shoot.get("center_motion", 0.0)),
        "killfeed_density": float(killfeed),
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
                video_id, vod, peak, label, "part_feedback", 1.0
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


def _best_threshold(y_true: list[int], probabilities: list[float]) -> float:
    if not y_true or len(set(y_true)) < 2:
        return 0.5
    best = (0.0, 0.5)
    for threshold in [x / 100 for x in range(30, 76, 5)]:
        pred = [int(p >= threshold) for p in probabilities]
        tp = sum(1 for y, p in zip(y_true, pred) if y == p == 1)
        fp = sum(1 for y, p in zip(y_true, pred) if y == 0 and p == 1)
        fn = sum(1 for y, p in zip(y_true, pred) if y == 1 and p == 0)
        precision = tp / max(tp + fp, 1)
        recall = tp / max(tp + fn, 1)
        f1 = 2 * precision * recall / max(precision + recall, 1e-9)
        if (f1, threshold) > best:
            best = (f1, threshold)
    return best[1]


def train(*, if_changed: bool = False) -> dict[str, Any]:
    import joblib
    import numpy as np
    from sklearn.linear_model import LogisticRegression
    from sklearn.metrics import balanced_accuracy_score
    from sklearn.model_selection import LeaveOneGroupOut
    from sklearn.pipeline import Pipeline
    from sklearn.preprocessing import StandardScaler

    output = model_path()
    signature = training_signature()
    if if_changed and output.exists():
        try:
            old = joblib.load(output)
            if old.get("training_signature") == signature:
                return {"status": "unchanged", "path": str(output)}
        except Exception:
            pass

    samples = load_training_samples()
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
                extract_features(row.video_path, max(0.0, row.peak_sec - 7.0), 14.0)
            )
            for row in samples
        ],
        dtype=np.float32,
    )
    y = np.asarray([row.label for row in samples], dtype=np.int32)
    groups = np.asarray([row.video_id for row in samples])
    weights = np.asarray([row.weight for row in samples], dtype=np.float32)

    def make_model() -> Pipeline:
        return Pipeline(
            [
                ("scale", StandardScaler()),
                ("model", LogisticRegression(max_iter=1000, class_weight="balanced")),
            ]
        )

    oof_y: list[int] = []
    oof_p: list[float] = []
    if len(set(groups)) >= 3:
        for train_idx, test_idx in LeaveOneGroupOut().split(X, y, groups):
            if len(set(y[train_idx])) < 2:
                continue
            fold = make_model()
            fold.fit(X[train_idx], y[train_idx], model__sample_weight=weights[train_idx])
            oof_y.extend(int(v) for v in y[test_idx])
            oof_p.extend(float(v) for v in fold.predict_proba(X[test_idx])[:, 1])
    threshold = _best_threshold(oof_y, oof_p)
    oof_balanced_accuracy = None
    if oof_y and len(set(oof_y)) == 2:
        oof_balanced_accuracy = float(
            balanced_accuracy_score(oof_y, [int(p >= threshold) for p in oof_p])
        )

    model = make_model()
    model.fit(X, y, model__sample_weight=weights)
    artifact = {
        "artifact_version": 1,
        "feature_version": FEATURE_VERSION,
        "feature_names": FEATURE_NAMES,
        "model": model,
        "threshold": threshold,
        "training_signature": signature,
        "trained_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "samples": len(samples),
        "positive": positives,
        "negative": negatives,
        "groups": len(set(groups)),
        "oof_balanced_accuracy": oof_balanced_accuracy,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=output.parent, suffix=".joblib", delete=False) as handle:
        tmp_path = Path(handle.name)
    try:
        joblib.dump(artifact, tmp_path)
        os.replace(tmp_path, output)
    finally:
        tmp_path.unlink(missing_ok=True)
    return {key: value for key, value in artifact.items() if key != "model"} | {
        "status": "trained",
        "path": str(output),
    }


def _load_artifact() -> dict | None:
    path = model_path()
    if not path.is_file():
        return None
    try:
        import joblib

        artifact = joblib.load(path)
    except Exception:
        return None
    if tuple(artifact.get("feature_names") or ()) != FEATURE_NAMES:
        return None
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
    scored: list[tuple[float, int, float]] = []
    for index, peak in enumerate(peaks[: max(1, cap)]):
        start = max(0.0, float(peak) - part_sec * 0.5)
        try:
            score = predict_score(video_path, start, part_sec)
        except Exception:
            score = None
        scored.append((float(score) if score is not None else -1.0, index, float(peak)))
    scored.sort(key=lambda row: (-row[0], row[1]))
    ranked = [peak for _score, _index, peak in scored]
    ranked.extend(float(peak) for peak in peaks[len(scored) :])
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
