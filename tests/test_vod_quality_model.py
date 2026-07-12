"""Tests for cross-game VOD feedback and quality models."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from vod_owner_feedback import invalidate_vod_feedback, record_owner_feedback  # noqa: E402
from vod_quality_model import quality_gate, train_and_promote, training_ready  # noqa: E402


def _configure_pubg(tmp_path: Path, monkeypatch) -> Path:
    root = tmp_path / "pubg"
    monkeypatch.setenv("SHOOTER_PUBG_DATA_ROOT", str(root))
    monkeypatch.setenv("VOD_PUBG_QUALITY_MODEL_PATH", str(root / "quality.joblib"))
    return root


def test_cross_game_feedback_invalidates_pool(tmp_path: Path, monkeypatch) -> None:
    root = _configure_pubg(tmp_path, monkeypatch)
    root.mkdir()
    (root / "vod_segment_state.json").write_text(
        json.dumps(
            {
                "vods": [
                    {
                        "id": "abcdefghijk",
                        "path": "/vods/yt_abcdefghijk.mp4",
                        "last_pool_peaks": [{"peak_sec": 10}],
                        "last_pool_at": 1,
                        "exhausted": True,
                    }
                ]
            }
        )
    )
    assert invalidate_vod_feedback("pubg", "abcdefghijk") == 1
    row = json.loads((root / "vod_segment_state.json").read_text())["vods"][0]
    assert "last_pool_peaks" not in row
    assert row["exhausted"] is False

    result = record_owner_feedback(
        "pubg",
        video_id="abcdefghijk",
        time_sec=12,
        label="bad",
        reason="running",
        item_id="abcdefghijk_8",
    )
    assert result["version"] == 1
    manifest = json.loads((root / "owner_feedback_manifest.json").read_text())
    assert manifest["counts"]["vod_segment:bad"] == 1


def test_pubg_model_trains_only_with_balanced_history(tmp_path: Path, monkeypatch) -> None:
    root = _configure_pubg(tmp_path, monkeypatch)
    root.mkdir()
    good: list[dict] = []
    bad: list[dict] = []
    segments: list[dict] = []
    for i in range(16):
        vid = f"pubg{i:07d}"[-11:]
        for label, bucket, suffix in ((1, good, 10), (0, bad, 50)):
            sid = f"{vid}_{i * 100 + suffix}"
            bucket.append({"segment_id": sid, "vod_id": vid})
            segments.append(
                {
                    "segment_id": sid,
                    "vod_id": vid,
                    "score": 0.9 if label else 0.1,
                    "clip_score": 0.8 if label else 0.1,
                    "panns_gunshot": 0.7 if label else 0.01,
                    "center_motion": 0.3 if label else 0.01,
                    "hit_flash": 0.2 if label else 0.0,
                    "visual_pass": bool(label),
                }
            )
    (root / "vod_segment_labels.json").write_text(json.dumps({"good": good, "bad": bad}))
    (root / "vod_segment_index.json").write_text(json.dumps({"segments": segments}))
    monkeypatch.setenv("VOD_TRAIN_MIN_PRECISION", "0.85")
    monkeypatch.setenv("VOD_TRAIN_MIN_RECALL", "0.70")
    monkeypatch.setenv("VOD_TRAIN_MAX_BAD_FALSE_PASS", "0.10")

    assert training_ready("pubg") is True
    passed, report = train_and_promote("pubg")
    assert passed is True
    assert report["holdout"]["precision"] == 1.0
    ok, reason, probability = quality_gate("pubg", segments[0])
    assert ok is True
    assert reason.startswith("quality_model_pass")
    assert probability > 0.5
