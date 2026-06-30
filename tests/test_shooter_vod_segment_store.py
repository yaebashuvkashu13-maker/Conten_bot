"""Tests for shooter VOD segment store feedback."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

from shooter_vod_segment_store import (  # noqa: E402
    apply_owner_label,
    inline_keyboard_markup,
    stats,
    upsert_segment,
)


@pytest.fixture
def pubg_data(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = tmp_path / "pubg"
    seg = root / "segments" / "seg_abc123_42.mp4"
    seg.parent.mkdir(parents=True)
    seg.write_bytes(b"fake")
    monkeypatch.setenv("SHOOTER_PUBG_DATA_ROOT", str(root))
    monkeypatch.setenv("SHOOTER_PUBG_SEGMENTS_ROOT", str(root / "segments"))
    monkeypatch.setenv("HIGHLIGHT_EXEMPLAR_ROOT", str(tmp_path / "exemplars"))
    monkeypatch.setenv("PUBG_OWNER_LABELS_PATH", str(tmp_path / "pubg_owner_labels.json"))
    monkeypatch.setenv("CONTENT_BOT_REPO", str(tmp_path))
    return root


def test_pubg_keyboard_uses_game_prefix(pubg_data: Path) -> None:
    markup = inline_keyboard_markup("pubg", "abc123_42")
    row = markup["inline_keyboard"][0]
    assert row[0]["callback_data"] == "pubg_vseg_yes:abc123_42"
    assert row[1]["callback_data"] == "pubg_vseg_no:abc123_42"


def test_apply_owner_label_records_feedback(pubg_data: Path) -> None:
    upsert_segment(
        "pubg",
        {
            "segment_id": "abc123_42",
            "path": str(pubg_data / "segments" / "seg_abc123_42.mp4"),
            "vod": "/tmp/vod.mp4",
            "start": 42,
            "peak_start": 46,
            "score": 0.8,
        },
    )
    ok, label = apply_owner_label("pubg", "abc123_42", is_good=True, by_chat="1")
    assert ok is True
    assert label == "good"
    s = stats("pubg")
    assert s["feedback_yes"] == 1
    labels = json.loads((pubg_data / "vod_segment_labels.json").read_text(encoding="utf-8"))
    assert len(labels["good"]) == 1
    assert labels["good"][0].get("exemplar", "").endswith("vod_abc123_42.mp4")


def test_apply_owner_label_writes_owner_json_and_exemplar(
    pubg_data: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    exemplars = tmp_path / "exemplars"
    owner = tmp_path / "pubg_owner_labels.json"
    monkeypatch.setenv("HIGHLIGHT_EXEMPLAR_ROOT", str(exemplars))
    monkeypatch.setenv("PUBG_OWNER_LABELS_PATH", str(owner))
    upsert_segment(
        "pubg",
        {
            "segment_id": "abc123_42",
            "path": str(pubg_data / "segments" / "seg_abc123_42.mp4"),
            "vod": str(pubg_data / "inbox" / "yt_abc12345678.mp4"),
            "start": 38,
            "peak_start": 42,
            "score": 0.8,
        },
    )
    ok, label = apply_owner_label("pubg", "abc123_42", is_good=False, reason="loot", by_chat="1")
    assert ok and label == "bad"
    assert (exemplars / "pubg" / "bad" / "vod_abc123_42.mp4").exists()
    data = json.loads(owner.read_text(encoding="utf-8"))
    assert data["videos"]["abc12345678"][0]["label"] == "bad"
    assert data["videos"]["abc12345678"][0]["note"] == "loot"
