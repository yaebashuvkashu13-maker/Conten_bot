"""Tests for the fast historical VOD quality model."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from mlbb_vod_quality_model import (  # noqa: E402
    clear_model_cache,
    quality_gate,
    quality_features,
    train_and_promote,
)


def test_quality_features_include_missingness() -> None:
    values = quality_features({"score": 0.2, "hook_score": 0.8})
    assert len(values) == 9
    assert values[-2:] == [0.0, 0.0]


def test_missing_required_model_fails_closed(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("MLBB_VOD_QUALITY_MODEL_PATH", str(tmp_path / "missing.joblib"))
    monkeypatch.setenv("MLBB_VOD_QUALITY_MODEL", "1")
    monkeypatch.setenv("MLBB_VOD_QUALITY_MODEL_REQUIRED", "1")
    clear_model_cache()
    ok, reason, probability = quality_gate({"score": 1})
    assert ok is False
    assert reason == "quality_model_missing"
    assert probability == 0.0


def test_train_and_promote_on_vod_grouped_history(tmp_path: Path, monkeypatch) -> None:
    good: list[dict] = []
    bad: list[dict] = []
    segments: list[dict] = []
    for group_index in range(16):
        vid = f"video{group_index:06d}"[-11:]
        good_sid = f"{vid}_{group_index * 100 + 10}"
        bad_sid = f"{vid}_{group_index * 100 + 50}"
        good.append({"segment_id": good_sid, "vod_id": vid})
        bad.append({"segment_id": bad_sid, "vod_id": vid})
        segments.extend(
            [
                {
                    "segment_id": good_sid,
                    "vod_id": vid,
                    "score": 0.9,
                    "hook_score": 0.9,
                    "clip_score": 0.8,
                    "fight_dur": 40,
                    "duration": 42,
                    "kill_banner": "triple",
                    "kill_banner_tier": 3,
                },
                {
                    "segment_id": bad_sid,
                    "vod_id": vid,
                    "score": 0.1,
                    "hook_score": 0.05,
                    "clip_score": 0.2,
                    "fight_dur": 10,
                    "duration": 12,
                },
            ]
        )
    (tmp_path / "vod_segment_labels.json").write_text(json.dumps({"good": good, "bad": bad}))
    (tmp_path / "vod_segment_index.json").write_text(json.dumps({"segments": segments}))
    model = tmp_path / "quality.joblib"
    monkeypatch.setenv("MLBB_DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("MLBB_VOD_QUALITY_MODEL_PATH", str(model))
    monkeypatch.setenv("MLBB_TRAIN_MIN_PRECISION", "0.85")
    monkeypatch.setenv("MLBB_TRAIN_MIN_RECALL", "0.70")
    monkeypatch.setenv("MLBB_TRAIN_MAX_BAD_FALSE_PASS", "0.10")

    passed, report = train_and_promote()
    assert passed is True
    assert report["holdout"]["precision"] == 1.0
    assert report["holdout"]["bad_false_pass"] == 0.0
    assert model.exists()
