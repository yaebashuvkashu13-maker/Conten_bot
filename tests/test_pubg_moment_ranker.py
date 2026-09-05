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
    monkeypatch.setenv("PUBG_RANKER_OWNER_AUGMENT_SEC", "0")
    monkeypatch.setenv("PUBG_RANKER_FEEDBACK_AUGMENT_SEC", "0")

    loaded = ranker.load_training_samples()
    assert len(loaded) == 1
    assert loaded[0].label == 0
    assert loaded[0].source == "part_feedback"


def test_owner_labels_are_time_augmented(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = tmp_path / "repo"
    samples = repo / "data" / "samples"
    samples.mkdir(parents=True)
    (samples / "yt_abcdefghijk.mp4").write_bytes(b"vod")
    owner = repo / "data" / "pubg_owner_labels.json"
    owner.write_text(
        json.dumps({"videos": {"abcdefghijk": [{"time_sec": 100, "label": "good"}]}})
    )
    feedback = tmp_path / "feedback.json"
    feedback.write_text("{}")
    monkeypatch.setenv("CONTENT_BOT_REPO", str(repo))
    monkeypatch.setenv("PUBG_OWNER_LABELS_PATH", str(owner))
    monkeypatch.setenv("PUBG_SEGMENT_LABELS_PATH", str(feedback))
    monkeypatch.setenv("PUBG_RANKER_OWNER_AUGMENT_SEC", "20")
    monkeypatch.setenv("PUBG_RANKER_FEEDBACK_AUGMENT_SEC", "0")
    loaded = ranker.load_training_samples()
    assert [row.peak_sec for row in loaded] == [80.0, 100.0, 120.0]
    assert loaded[1].weight > loaded[0].weight


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


def test_quality_report_reuses_training_features() -> None:
    features = ranker.features_from_quality_report(
        {"panns_gun_max": 0.5, "gunfire_density": 0.08}
    )
    assert features is not None
    assert features["panns_gun_max"] == 0.5
    assert features["gunfire_density"] == 0.08


def test_ranker_budget_includes_timeline_diversity(monkeypatch: pytest.MonkeyPatch) -> None:
    import numpy as np

    peaks = [float(index * 10) for index in range(20)] + [900.0]
    class FakeModel:
        def predict_proba(self, rows):
            return np.asarray([[1.0 - row[0], row[0]] for row in rows])

    monkeypatch.setattr(ranker, "_load_artifact", lambda: {"model": FakeModel()})
    seen: list[float] = []

    def extract(_path: Path, start: float, _duration: float):
        peak = start + 7.0
        seen.append(peak)
        value = 0.99 if peak == 900.0 else 0.1
        return {name: value for name in ranker.FEATURE_NAMES}

    monkeypatch.setattr(ranker, "extract_features", extract)
    ranked, _ = ranker.rank_peaks_with_model(
        Path("/tmp/vod.mp4"),
        peaks,
        part_sec=14.0,
        max_probes=6,
    )
    assert 900.0 in seen
    assert ranked[0] == 900.0
    assert set(ranked) == set(peaks)
