#!/usr/bin/env python3
"""Train LogisticRegression meta-model on owner labels + exemplar clips (~1h setup)."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

from highlight_scorer import CLASSIFIER_PATH, OWNER_LABELS, WINDOW_SEC, score_candidate_window

REPO = Path(__file__).resolve().parent.parent
INBOX = Path(os.environ.get("HIGHLIGHT_INBOX", "/root/data/mlbb/youtube_nightly/inbox"))


def load_owner_samples(vod: Path) -> list[tuple[float, int]]:
    if not OWNER_LABELS.exists():
        return []
    data = json.loads(OWNER_LABELS.read_text(encoding="utf-8"))
    vid = vod.stem[3:] if vod.stem.startswith("yt_") else vod.stem
    rows = data.get("videos", {}).get(vid, [])
    out: list[tuple[float, int]] = []
    for row in rows:
        if "time_sec" not in row:
            continue
        label = 1 if row.get("label") == "good" else 0
        out.append((float(row["time_sec"]) - WINDOW_SEC * 0.5, label))
    return out


def extract_features(vod: Path, start: float, profile: str) -> list[float]:
    m = score_candidate_window(vod, max(0, start), WINDOW_SEC, profile)
    return [
        m.panns_gunshot,
        m.panns_machine_gun,
        m.panns_explosion,
        m.clip_score,
        m.center_motion,
        m.boss_bar,
    ]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--vod", default="yt_n97cHIR9Qow.mp4")
    parser.add_argument("--profile", default="pubg")
    args = parser.parse_args()

    vod = INBOX / args.vod if not Path(args.vod).exists() else Path(args.vod)
    if not vod.exists():
        vod = REPO / "data" / "samples" / args.vod
    if not vod.exists():
        print(f"REFUSED: train, reason=vod_missing {args.vod}")
        return 1

    X: list[list[float]] = []
    y: list[int] = []
    for start, label in load_owner_samples(vod):
        X.append(extract_features(vod, start, args.profile))
        y.append(label)

    exemplar_root = REPO / "data" / "highlight_exemplars" / args.profile
    for label_name, cls in (("good", 1), ("bad", 0)):
        folder = exemplar_root / label_name
        if not folder.exists():
            continue
        for clip in sorted(folder.glob("*.mp4"))[:30]:
            X.append(extract_features(clip, 0.5, args.profile))
            y.append(cls)

    if len(X) < 8:
        print(f"REFUSED: train, reason=insufficient_samples n={len(X)}")
        return 1

    from sklearn.linear_model import LogisticRegression
    import joblib

    clf = LogisticRegression(max_iter=500, class_weight="balanced")
    clf.fit(np.array(X), np.array(y))
    CLASSIFIER_PATH.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(clf, CLASSIFIER_PATH)
    acc = clf.score(np.array(X), np.array(y))
    print(f"OK classifier saved {CLASSIFIER_PATH} samples={len(X)} train_acc={acc:.3f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
