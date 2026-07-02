"""Tests for cross-game VOD owner learning (exemplars + time anchors)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

from vod_owner_learning import (  # noqa: E402
    append_owner_time_label,
    backfill_owner_labels_from_vod_segments,
    labels_from_vod_segment_store,
    owner_labels_for_vod_scan,
)


@pytest.fixture
def pubg_learning(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    data_root = tmp_path / "pubg"
    data_root.mkdir(parents=True)
    exemplars = tmp_path / "exemplars"
    owner = tmp_path / "pubg_owner_labels.json"
    labels = data_root / "vod_segment_labels.json"
    labels.write_text(
        json.dumps(
            {
                "good": [
                    {
                        "segment_id": "abc12345678_120",
                        "vod": str(data_root / "inbox" / "yt_abc12345678.mp4"),
                        "start": 116,
                        "peak_start": 120,
                    }
                ],
                "bad": [],
                "feedback": [],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("SHOOTER_PUBG_DATA_ROOT", str(data_root))
    monkeypatch.setenv("PUBG_OWNER_LABELS_PATH", str(owner))
    monkeypatch.setenv("HIGHLIGHT_EXEMPLAR_ROOT", str(exemplars))
    return data_root, owner, exemplars


def test_labels_from_vod_segment_store(pubg_learning) -> None:
    data_root, _owner, _ex = pubg_learning
    vod = data_root / "inbox" / "yt_abc12345678.mp4"
    vod.parent.mkdir(parents=True, exist_ok=True)
    vod.write_bytes(b"x")

    rows = labels_from_vod_segment_store(vod, "pubg")
    assert len(rows) == 1
    assert rows[0]["label"] == "good"
    assert rows[0]["time_sec"] == 120.0


def test_owner_labels_for_vod_scan_merges_json_and_segments(pubg_learning) -> None:
    data_root, owner, _ex = pubg_learning
    vod = data_root / "inbox" / "yt_abc12345678.mp4"
    vod.parent.mkdir(parents=True, exist_ok=True)
    vod.write_bytes(b"x")
    owner.write_text(
        json.dumps({"videos": {"abc12345678": [{"time_sec": 50.0, "label": "bad", "source": "manual"}]}}),
        encoding="utf-8",
    )

    rows = owner_labels_for_vod_scan(vod, "pubg")
    labels = {r["label"] for r in rows}
    assert "good" in labels
    assert "bad" in labels


def test_backfill_owner_labels_from_vod_segments(pubg_learning) -> None:
    _data_root, owner, _ex = pubg_learning
    n = backfill_owner_labels_from_vod_segments("pubg")
    assert n == 1
    data = json.loads(owner.read_text(encoding="utf-8"))
    assert "abc12345678" in data["videos"]
    assert data["videos"]["abc12345678"][0]["time_sec"] == 120.0
    assert not append_owner_time_label(
        "pubg",
        "abc12345678",
        120.0,
        "good",
        source="vod_segment_backfill",
    )
