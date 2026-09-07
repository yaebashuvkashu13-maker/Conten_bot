#!/usr/bin/env python3
"""Tests for the PUBG learnable dataset / window-label loop."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import pubg_ranker_dataset as dataset  # noqa: E402
import pubg_window_label_queue as queue  # noqa: E402


def test_append_window_label_writes_jsonl(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    labels = tmp_path / "windows.jsonl"
    monkeypatch.setenv("PUBG_WINDOW_LABELS_PATH", str(labels))
    monkeypatch.setenv("VOD_RUNTIME_LABELS", "0")
    # Avoid touching real owner JSON if mirror fails safely.
    monkeypatch.setattr(
        "vod_owner_learning.append_owner_time_label",
        lambda *a, **k: True,
        raising=False,
    )
    row = dataset.append_window_label(
        video_id="abcdefghijk",
        t0=100.0,
        t1=115.0,
        label="good",
        event="fight",
        path=labels,
    )
    assert row["label"] == "good"
    assert row["peak_sec"] == 107.5
    assert row["event"] == "fight"
    loaded = dataset.load_window_label_rows(labels)
    assert len(loaded) == 1
    assert loaded[0]["label"] == 1
    assert loaded[0]["source"] == "window_label"


def test_export_dataset_dedupes_and_includes_windows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    samples = repo / "data" / "samples"
    samples.mkdir(parents=True)
    vod = samples / "yt_abcdefghijk.mp4"
    vod.write_bytes(b"vod")
    owner = repo / "data" / "pubg_owner_labels.json"
    owner.write_text(
        json.dumps({"videos": {"abcdefghijk": [{"time_sec": 100, "label": "good"}]}}),
        encoding="utf-8",
    )
    feedback = tmp_path / "feedback.json"
    feedback.write_text("{}", encoding="utf-8")
    windows = tmp_path / "windows.jsonl"
    windows.write_text(
        json.dumps(
            {
                "video_id": "abcdefghijk",
                "t0": 200,
                "t1": 215,
                "peak_sec": 207.5,
                "label": "bad",
                "source": "window_label",
                "weight": 1.5,
                "video_path": str(vod),
            }
        )
        + "\n",
        encoding="utf-8",
    )
    out = tmp_path / "dataset.jsonl"
    monkeypatch.setenv("CONTENT_BOT_REPO", str(repo))
    monkeypatch.setenv("PUBG_OWNER_LABELS_PATH", str(owner))
    monkeypatch.setenv("PUBG_SEGMENT_LABELS_PATH", str(feedback))
    monkeypatch.setenv("PUBG_WINDOW_LABELS_PATH", str(windows))
    monkeypatch.setenv("PUBG_RANKER_OWNER_AUGMENT_SEC", "0")
    monkeypatch.setenv("PUBG_RANKER_FEEDBACK_AUGMENT_SEC", "0")
    monkeypatch.setenv("VOD_RUNTIME_LABELS", "0")

    report = dataset.export_dataset(output=out, include_windows=True)
    assert report["rows"] >= 2
    rows = dataset.load_jsonl(out)
    peaks = {round(float(r["peak_sec"]), 1) for r in rows}
    assert 100.0 in peaks
    assert 207.5 in peaks


def test_samples_from_dataset_uses_cached_features(tmp_path: Path) -> None:
    path = tmp_path / "ds.jsonl"
    feats = {
        "panns_gunshot": 0.8,
        "panns_machine_gun": 0.7,
        "panns_explosion": 0.1,
        "panns_speech": 0.0,
        "panns_music": 0.0,
        "panns_gun_max": 0.8,
        "gunfire_density": 0.2,
        "burst_ratio": 8.0,
        "audio_rms": 0.4,
    }
    path.write_text(
        json.dumps(
            {
                "video_id": "abcdefghijk",
                "peak_sec": 50,
                "label": 1,
                "source": "dataset",
                "weight": 1.0,
                "features": feats,
            }
        )
        + "\n"
        + json.dumps(
            {
                "video_id": "abcdefghijk",
                "peak_sec": 120,
                "label": 0,
                "source": "dataset",
                "weight": 1.0,
                "features": {**feats, "gunfire_density": 0.01, "burst_ratio": 0.2},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    samples = dataset.samples_from_dataset(path)
    assert len(samples) == 2
    assert samples[0].features is not None
    assert samples[0].label == 1
    assert samples[1].label == 0


def test_build_queue_grid_without_ffprobe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    vod = tmp_path / "yt_abcdefghijk.mp4"
    vod.write_bytes(b"x")
    qpath = tmp_path / "queue.jsonl"
    monkeypatch.setenv("PUBG_WINDOW_LABEL_QUEUE", str(qpath))
    monkeypatch.setattr(queue, "_ffprobe_duration", lambda _p: 600.0)
    monkeypatch.setattr(queue, "_dense_peaks", lambda _p, limit=40: [120.0, 300.0])
    report = queue.build_queue(vod, window_sec=15.0, grid_stride_sec=60.0, max_windows=20)
    assert report["status"] == "queued"
    assert report["rows"] >= 2
    assert qpath.is_file()
