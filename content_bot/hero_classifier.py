from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from pathlib import Path

import joblib
import pandas as pd
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import classification_report
from sklearn.model_selection import train_test_split

from .video_features import extract_video_features


@dataclass(slots=True)
class TrainResult:
    positive_count: int
    negative_count: int
    feature_count: int
    holdout_size: int
    report: dict


def _gather_rows(input_dir: str | Path, label: str) -> list[dict]:
    rows: list[dict] = []
    for video_file in sorted(Path(input_dir).rglob("*.mp4")):
        features = extract_video_features(video_file)
        rows.append({"path": str(video_file), "label": label, **features})
    return rows


def discover_labeled_videos(data_dir: str | Path) -> list[tuple[Path, str]]:
    """Discover videos under labeled folder tree.

    Layouts:
      data_dir/<hero>/*.mp4           -> label <hero>
      data_dir/<hero>/<skin>/*.mp4    -> label <hero>/<skin>
    """
    root = Path(data_dir)
    if not root.exists():
        return []

    pairs: list[tuple[Path, str]] = []
    for hero_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        direct_videos = list(hero_dir.glob("*.mp4"))
        if direct_videos:
            for video in sorted(direct_videos):
                pairs.append((video, hero_dir.name))
            continue
        for skin_dir in sorted(p for p in hero_dir.iterdir() if p.is_dir()):
            for video in sorted(skin_dir.glob("*.mp4")):
                pairs.append((video, f"{hero_dir.name}/{skin_dir.name}"))
    return pairs


def _gather_labeled_rows(data_dir: str | Path) -> list[dict]:
    pairs = discover_labeled_videos(data_dir)
    if not pairs:
        raise RuntimeError(f"No labeled videos found under {data_dir}")

    rows: list[dict] = []
    for index, (video_file, label) in enumerate(pairs, start=1):
        print(f"[{index}/{len(pairs)}] {label}: {video_file.name}")
        features = extract_video_features(video_file)
        rows.append({"path": str(video_file), "label": label, **features})
    return rows


def train_multiclass_classifier(
    data_dir: str | Path,
    output_dir: str | Path,
    *,
    features_csv: str | Path | None = None,
) -> TrainResult:
    if features_csv:
        df = pd.read_csv(features_csv)
        if "label" not in df.columns:
            raise RuntimeError("features_csv must contain a label column.")
    else:
        rows = _gather_labeled_rows(data_dir)
        df = pd.DataFrame(rows)

    label_counts = df["label"].value_counts()
    if len(label_counts) < 2:
        raise RuntimeError("Need at least 2 classes to train multiclass classifier.")
    if label_counts.min() < 2:
        weak = label_counts[label_counts < 2].index.tolist()
        print(f"WARNING: classes with <2 samples (may hurt accuracy): {weak}")

    feature_columns = [col for col in df.columns if col not in {"path", "label"}]
    split_kwargs = {"test_size": 0.25, "random_state": 42}
    try:
        train_df, test_df = train_test_split(df, stratify=df["label"], **split_kwargs)
    except ValueError:
        print("WARNING: stratified split failed; using random split.")
        train_df, test_df = train_test_split(df, **split_kwargs)

    model = RandomForestClassifier(
        n_estimators=400,
        random_state=42,
        class_weight="balanced_subsample",
        min_samples_leaf=1,
    )
    model.fit(train_df[feature_columns], train_df["label"])
    predictions = model.predict(test_df[feature_columns])
    report = classification_report(test_df["label"], predictions, output_dict=True)

    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    joblib.dump(model, output_path / "hero_classifier.joblib")
    (output_path / "feature_columns.json").write_text(json.dumps(feature_columns, ensure_ascii=False, indent=2))
    (output_path / "classification_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
    label_counts.to_csv(output_path / "label_counts.csv")

    return TrainResult(
        positive_count=len(df),
        negative_count=len(label_counts),
        feature_count=len(feature_columns),
        holdout_size=len(test_df),
        report=report,
    )


def train_classifier(
    positive_dir: str | Path,
    negative_dir: str | Path,
    output_dir: str | Path,
) -> TrainResult:
    positive_rows = _gather_rows(positive_dir, "positive")
    negative_rows = _gather_rows(negative_dir, "negative")
    if not positive_rows or not negative_rows:
        raise RuntimeError("Need both positive and negative videos to train classifier.")

    rows = positive_rows + negative_rows
    df = pd.DataFrame(rows)
    feature_columns = [col for col in df.columns if col not in {"path", "label"}]

    train_df, test_df = train_test_split(
        df,
        test_size=0.25,
        random_state=42,
        stratify=df["label"],
    )

    model = RandomForestClassifier(
        n_estimators=300,
        random_state=42,
        class_weight="balanced",
        min_samples_leaf=1,
    )
    model.fit(train_df[feature_columns], train_df["label"])
    predictions = model.predict(test_df[feature_columns])
    report = classification_report(test_df["label"], predictions, output_dict=True)

    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    joblib.dump(model, output_path / "hero_classifier.joblib")
    (output_path / "feature_columns.json").write_text(json.dumps(feature_columns, ensure_ascii=False, indent=2))
    (output_path / "classification_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))

    return TrainResult(
        positive_count=len(positive_rows),
        negative_count=len(negative_rows),
        feature_count=len(feature_columns),
        holdout_size=len(test_df),
        report=report,
    )


def score_directory(model_dir: str | Path, input_dir: str | Path, output_csv: str | Path) -> int:
    model_path = Path(model_dir)
    model = joblib.load(model_path / "hero_classifier.joblib")
    feature_columns = json.loads((model_path / "feature_columns.json").read_text())
    classes = list(model.classes_)
    binary_positive = "positive" if "positive" in classes else None

    rows = []
    for video_file in sorted(Path(input_dir).rglob("*.mp4")):
        features = extract_video_features(video_file)
        row = {"path": str(video_file), **features}
        df = pd.DataFrame([{col: row.get(col, 0.0) for col in feature_columns}])
        proba = model.predict_proba(df)[0]
        predicted_index = int(proba.argmax())
        row["predicted_label"] = classes[predicted_index]
        row["predicted_probability"] = float(proba[predicted_index])
        if binary_positive is not None:
            row["positive_probability"] = float(proba[classes.index(binary_positive)])
        rows.append(row)

    if not rows:
        return 0

    out = Path(output_csv)
    out.parent.mkdir(parents=True, exist_ok=True)
    sort_col = "positive_probability" if binary_positive else "predicted_probability"
    pd.DataFrame(rows).sort_values(sort_col, ascending=False).to_csv(out, index=False)
    return len(rows)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Train and score a weak hero classifier.")
    sub = parser.add_subparsers(dest="command", required=True)

    train = sub.add_parser("train")
    train.add_argument("--positive-dir", required=True)
    train.add_argument("--negative-dir", required=True)
    train.add_argument("--output-dir", required=True)

    score = sub.add_parser("score")
    score.add_argument("--model-dir", required=True)
    score.add_argument("--input-dir", required=True)
    score.add_argument("--output-csv", required=True)

    multi = sub.add_parser("train-multiclass", help="Train hero/skin classifier from folder labels.")
    multi.add_argument("--data-dir", default="datasets/labeled")
    multi.add_argument("--features-csv", help="Optional precomputed features CSV from batch_features.")
    multi.add_argument("--output-dir", required=True)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if args.command == "train":
        result = train_classifier(args.positive_dir, args.negative_dir, args.output_dir)
        print(json.dumps(asdict(result), ensure_ascii=False, indent=2))
        return 0
    if args.command == "score":
        count = score_directory(args.model_dir, args.input_dir, args.output_csv)
        print(f"Scored {count} videos.")
        return 0
    if args.command == "train-multiclass":
        result = train_multiclass_classifier(
            args.data_dir,
            args.output_dir,
            features_csv=args.features_csv,
        )
        print(json.dumps(asdict(result), ensure_ascii=False, indent=2))
        return 0
    raise RuntimeError(f"Unsupported command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())

