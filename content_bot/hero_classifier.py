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

    stratify = df["label"]
    if df["label"].value_counts().min() < 2:
        stratify = None

    train_df, test_df = train_test_split(
        df,
        test_size=0.25,
        random_state=42,
        stratify=stratify,
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

    rows = []
    for video_file in sorted(Path(input_dir).rglob("*.mp4")):
        features = extract_video_features(video_file)
        row = {"path": str(video_file), **features}
        df = pd.DataFrame([{col: row.get(col, 0.0) for col in feature_columns}])
        proba = model.predict_proba(df)[0]
        classes = list(model.classes_)
        if "positive" in classes:
            row["positive_probability"] = float(proba[classes.index("positive")])
        else:
            row["positive_probability"] = float(max(proba))
        rows.append(row)

    if not rows:
        return 0

    out = Path(output_csv)
    out.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).sort_values("positive_probability", ascending=False).to_csv(out, index=False)
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
    raise RuntimeError(f"Unsupported command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())

