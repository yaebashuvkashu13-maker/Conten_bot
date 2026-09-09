#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import pubg_sent_param_ranges as ranges  # noqa: E402


def test_percentile_and_build_ranges(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ledger = tmp_path / "pubg_clip_ledger.jsonl"
    labels = tmp_path / "vod_segment_labels.json"
    out = tmp_path / "pubg_sent_ranges.json"
    monkeypatch.setenv("VOD_QUALITY_LEDGER_DIR", str(tmp_path))
    monkeypatch.setenv("PUBG_SEGMENT_LABELS_PATH", str(labels))
    monkeypatch.setenv("VOD_ADAPTIVE_THRESH_DIR", str(tmp_path))
    monkeypatch.setenv("PUBG_SENT_RANGE_MIN_SAMPLES", "5")

    rows = []
    for i, gun in enumerate([0.04, 0.05, 0.06, 0.07, 0.08, 0.09, 0.10]):
        cid = f"vid_{i}"
        metrics = {
            "gunfire_density": gun,
            "burst_ratio": 4.0 + i,
            "audio_rms": 0.2 + i * 0.01,
            "duration": 20.0 + i,
            "panns_gun_max": 0.5,
            "quality_score": 0.4,
        }
        rows.append(
            json.dumps(
                {
                    "decision": "sent",
                    "clip_id": cid,
                    "vod_id": "vid",
                    "metrics": metrics,
                }
            )
        )
        if i >= 3:
            rows.append(
                json.dumps(
                    {"decision": "feedback", "clip_id": cid, "label": "good", "reason": "good"}
                )
            )
    ledger.write_text("\n".join(rows) + "\n", encoding="utf-8")
    labels.write_text("{}", encoding="utf-8")

    payload = ranges.build_ranges("pubg")
    assert payload["ready"] is True
    assert "gunfire_density" in payload["ranges"]
    assert out.is_file()

    ok, reason, report = ranges.evaluate_against_ranges(
        {"gunfire_density": 0.075, "burst_ratio": 7.0, "audio_rms": 0.25, "duration": 24.0, "panns_gun_max": 0.5, "quality_score": 0.4}
    )
    assert ok, reason

    ok2, reason2, _ = ranges.evaluate_against_ranges(
        {"gunfire_density": 0.001, "burst_ratio": 0.1, "audio_rms": 0.01, "duration": 5.0}
    )
    assert not ok2
    assert "out_of_sent_range" in reason2


def test_flatten_metrics_aliases() -> None:
    flat = ranges.flatten_metrics({"gun_density": 0.1, "gun_burst_ratio": 5.0, "rms": 0.3})
    assert flat["gunfire_density"] == 0.1
    assert flat["burst_ratio"] == 5.0
    assert flat["audio_rms"] == 0.3
