#!/usr/bin/env python3
"""Train per-game LogisticRegression meta-model on owner labels + exemplar clips."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

from highlight_scorer import (
    WINDOW_SEC,
    _owner_labels_path,
    classifier_path_for_profile,
    normalize_profile,
    score_candidate_window,
)

PROFILE_ALIASES = {"mlbb": "mobile_legends", "world_of_tanks": "wot"}

LABEL_FILES = {
    "pubg": "pubg_owner_labels.json",
    "standoff": "standoff_owner_labels.json",
    "mobile_legends": "mobile_legends_owner_labels.json",
    "genshin": "genshin_owner_labels.json",
    "wot": "wot_owner_labels.json",
}


def _repo_root() -> Path:
    env = os.environ.get("CONTENT_BOT_REPO", "").strip()
    if env:
        return Path(env)
    root = Path(__file__).resolve().parent.parent
    if root.name == "bin" or str(root) == "/usr/local":
        return Path("/root/content_bot_ml")
    return root


REPO = _repo_root()
INBOX = Path(os.environ.get("HIGHLIGHT_INBOX", "/root/data/mlbb/youtube_nightly/inbox"))
DATA_MLBB = Path(os.environ.get("MLBB_DATA_ROOT", "/root/data/mlbb"))


def labels_path_for(profile: str) -> Path:
    profile = normalize_profile(profile)
    path = _owner_labels_path(profile)
    if path and path.exists():
        return path
    return REPO / "data" / LABEL_FILES.get(profile, "pubg_owner_labels.json")


def resolve_vod(video_id: str) -> Path | None:
    for candidate in (
        INBOX / f"yt_{video_id}.mp4",
        REPO / "data" / "samples" / f"yt_{video_id}.mp4",
        Path(f"/root/data/mlbb/youtube_nightly/inbox/yt_{video_id}.mp4"),
    ):
        if candidate.exists():
            return candidate
    return None


def load_mlbb_owner_calibration(profile: str) -> list[tuple[Path, float, int]]:
    """Shorts 👍/👎 + VOD segment 👍/👎 from Telegram calibration stores."""
    if normalize_profile(profile) != "mobile_legends":
        return []
    out: list[tuple[Path, float, int]] = []

    cal_path = DATA_MLBB / "calibration_labels.json"
    if cal_path.exists():
        try:
            data = json.loads(cal_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            data = {}
        for row in data.get("good", []):
            path = Path(str(row.get("path", "")))
            if path.exists():
                out.append((path, 0.15, 1))
        for row in data.get("bad", []):
            path = Path(str(row.get("path", "")))
            if path.exists():
                out.append((path, 0.15, 0))

    vseg_path = DATA_MLBB / "vod_segment_labels.json"
    if vseg_path.exists():
        try:
            vdata = json.loads(vseg_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            vdata = {}
        for row in vdata.get("good", []):
            path = Path(str(row.get("path", "")))
            if path.exists():
                out.append((path, 0.5, 1))
        for row in vdata.get("bad", []):
            path = Path(str(row.get("path", "")))
            if path.exists():
                out.append((path, 0.5, 0))

    return out


def load_all_owner_samples(profile: str) -> list[tuple[Path, float, int]]:
    """All (vod, start, label) from data/{game}_owner_labels.json."""
    path = labels_path_for(profile)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    videos = data.get("videos", data) if isinstance(data.get("videos"), dict) else {}
    if not isinstance(videos, dict):
        videos = data if isinstance(data, dict) else {}
    out: list[tuple[Path, float, int]] = []
    for vid, rows in videos.items():
        if not isinstance(rows, list):
            continue
        vod = resolve_vod(str(vid))
        if not vod:
            continue
        for row_index, row in enumerate(rows, start=1):
            if "time_sec" not in row:
                continue
            label = 1 if row.get("label") == "good" else 0
            start = float(row["time_sec"]) - WINDOW_SEC * 0.5
            out.append((vod, max(0.0, start), label))
    return out


def extract_features(vod: Path, start: float, profile: str) -> list[float]:
    m = score_candidate_window(vod, start, WINDOW_SEC, profile)
    if normalize_profile(profile) == "mobile_legends":
        from mlbb_owner_learning import mlbb_classifier_features

        return mlbb_classifier_features(m)
    return [
        m.panns_gunshot,
        m.panns_machine_gun,
        m.panns_explosion,
        m.clip_score,
        m.center_motion,
        m.boss_bar,
    ]


def train_profile(profile: str, *, max_exemplar: int = 30) -> int:
    profile = normalize_profile(profile)
    os.environ["HIGHLIGHT_TRAIN_MODE"] = "1"
    os.environ["HIGHLIGHT_USE_OWNER_ANCHORS"] = "0"
    os.environ["HIGHLIGHT_HEATMAP"] = "0"
    os.environ.setdefault("CONTENT_BOT_REPO", str(REPO))
    os.environ["_HIGHLIGHT_PROFILE"] = profile

    X: list[list[float]] = []
    y: list[int] = []
    groups: list[str] = []
    weights: list[float] = []
    if profile == "mobile_legends":
        from mlbb_model_training import (
            feature_key,
            load_feature_cache,
            save_feature_cache,
        )
        from mlbb_owner_learning import (
            build_training_manifest,
            load_unified_training_rows,
        )

        rows = load_unified_training_rows(profile)
        cache = load_feature_cache()
        cached_features = cache.setdefault("features", {})
        cache_changed = False
        for row in rows:
            path = Path(row["path"])
            start = float(row["start"])
            key = feature_key(path, start, profile)
            features = cached_features.get(key)
            if not isinstance(features, list):
                try:
                    features = extract_features(path, start, profile)
                except Exception as exc:
                    print(f"skip_feature path={path} start={start:.1f} error={exc}")
                    continue
                cached_features[key] = [float(value) for value in features]
                cache_changed = True
            X.append([float(value) for value in features])
            y.append(int(row["label"]))
            groups.append(str(row.get("group") or path.stem))
            weights.append(float(row.get("weight") or 1.0))
            if cache_changed and row_index % 25 == 0:
                save_feature_cache(cache)
                cache_changed = False
                print(
                    f"feature_progress={row_index}/{len(rows)} cached={len(cached_features)}",
                    flush=True,
                )
        if cache_changed:
            save_feature_cache(cache)
        manifest = build_training_manifest(profile)
        print(
            f"unified_mlbb_samples={len(rows)} usable={len(X)} "
            f"groups={len(set(groups))} dataset={manifest.get('dataset_version')}"
        )
    else:
        for vod, start, label in load_all_owner_samples(profile):
            X.append(extract_features(vod, start, profile))
            y.append(label)
            groups.append(vod.stem)
            weights.append(1.0)

    if profile != "mobile_legends":
        exemplar_root = REPO / "data" / "highlight_exemplars" / profile
        for label_name, cls in (("good", 1), ("bad", 0)):
            folder = exemplar_root / label_name
            if not folder.exists():
                continue
            for clip in sorted(folder.glob("*.mp4"))[:max_exemplar]:
                X.append(extract_features(clip, 0.5, profile))
                y.append(cls)
                groups.append(clip.stem)
                weights.append(1.0)

    if len(X) < 12 or len(set(y)) < 2:
        print(f"REFUSED: train profile={profile}, reason=insufficient_or_one_class n={len(X)}")
        return 1

    from sklearn.linear_model import LogisticRegression
    from mlbb_model_training import (
        evaluate_binary,
        grouped_holdout_indices,
        passes_quality_gate,
        promote_candidate,
        write_candidate,
    )

    X_arr = np.asarray(X, dtype=float)
    y_arr = np.asarray(y, dtype=int)
    weight_arr = np.asarray(weights, dtype=float)
    try:
        train_idx, test_idx = grouped_holdout_indices(y_arr, groups)
    except ValueError as exc:
        print(f"REFUSED: train profile={profile}, reason={exc}")
        return 1

    validation_model = LogisticRegression(max_iter=500, class_weight="balanced")
    validation_model.fit(
        X_arr[train_idx],
        y_arr[train_idx],
        sample_weight=weight_arr[train_idx],
    )
    metrics = evaluate_binary(validation_model, X_arr[test_idx], y_arr[test_idx])
    passed, failures = passes_quality_gate(metrics)
    out_path = classifier_path_for_profile(profile)
    final_model = LogisticRegression(max_iter=500, class_weight="balanced")
    final_model.fit(X_arr, y_arr, sample_weight=weight_arr)
    metadata = {
        "schema_version": 1,
        "profile": profile,
        "trained_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "samples": len(X),
        "groups": len(set(groups)),
        "features": 6 if profile == "mobile_legends" else len(X[0]),
        "holdout": metrics,
        "quality_pass": passed,
        "failures": failures,
        "sources": manifest.get("clip_ranker", {}).get("sources", {})
        if profile == "mobile_legends"
        else {},
        "dataset_version": manifest.get("dataset_version", "")
        if profile == "mobile_legends"
        else "",
    }
    candidate, _candidate_meta = write_candidate(final_model, out_path, metadata)
    if not passed:
        print(
            f"REFUSED: promote profile={profile} candidate={candidate} "
            f"metrics={metrics} failures={','.join(failures)}"
        )
        return 2
    promote_candidate(out_path, candidate, metadata)
    print(
        f"OK classifier profile={profile} path={out_path} samples={len(X)} "
        f"groups={len(set(groups))} holdout={metrics}"
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--profile",
        default="pubg",
        choices=["pubg", "standoff", "mobile_legends", "mlbb", "genshin", "wot", "all"],
    )
    parser.add_argument("--vod", default="", help="legacy single-vod hint (ignored if labels json has videos)")
    args = parser.parse_args()

    if args.profile == "all":
        code = 0
        for prof in ("pubg", "standoff", "mobile_legends", "genshin", "wot"):
            if train_profile(prof) != 0:
                code = 1
        return code
    return train_profile(normalize_profile(args.profile))


if __name__ == "__main__":
    raise SystemExit(main())
