from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import pubg_moment_ranker as ranker  # noqa: E402


def test_part_feedback_overrides_owner_label(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = tmp_path / "repo"
    samples = repo / "data" / "samples"
    samples.mkdir(parents=True)
    vod = samples / "yt_abcdefghijk.mp4"
    vod.write_bytes(b"vod")
    owner = repo / "data" / "pubg_owner_labels.json"
    owner.write_text(
        json.dumps(
            {"videos": {"abcdefghijk": [{"time_sec": 100, "label": "good"}]}}
        )
    )
    feedback = tmp_path / "feedback.json"
    feedback.write_text(
        json.dumps(
            {
                "good": [],
                "bad": [
                    {
                        "segment_id": "abcdefghijk_93",
                        "vod": str(vod),
                        "peak_start": 100,
                    }
                ],
            }
        )
    )
    monkeypatch.setenv("CONTENT_BOT_REPO", str(repo))
    monkeypatch.setenv("PUBG_OWNER_LABELS_PATH", str(owner))
    monkeypatch.setenv("PUBG_SEGMENT_LABELS_PATH", str(feedback))

    loaded = ranker.load_training_samples()
    assert len(loaded) == 1
    assert loaded[0].label == 0
    assert loaded[0].source == "part_feedback"


def test_train_and_rank_model(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    model = tmp_path / "ranker.joblib"
    monkeypatch.setenv("PUBG_RANKER_MODEL", str(model))
    rows = []
    for group in range(3):
        vod = tmp_path / f"yt_group{group:05d}.mp4"
        vod.write_bytes(b"vod")
        for index in range(4):
            label = index % 2
            rows.append(
                ranker.TrainingSample(
                    f"group{group:05d}",
                    vod,
                    float(group * 100 + index * 10),
                    label,
                    "owner_label",
                    2.0,
                )
            )
    monkeypatch.setattr(ranker, "load_training_samples", lambda: rows)
    monkeypatch.setattr(
        ranker,
        "extract_features",
        lambda _vod, start, _dur: {
            name: (1.0 if int((start + 7) / 10) % 2 else 0.0)
            for name in ranker.FEATURE_NAMES
        },
    )
    monkeypatch.setattr(ranker, "training_signature", lambda: "test-signature")

    report = ranker.train()
    assert report["status"] == "trained"
    assert report["samples"] == 12
    assert model.exists()
