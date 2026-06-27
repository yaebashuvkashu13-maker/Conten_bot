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
    dislike_picker_markup,
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
    owner = tmp_path / "pubg_owner_labels.json"
    monkeypatch.setenv("SHOOTER_PUBG_DATA_ROOT", str(root))
    monkeypatch.setenv("SHOOTER_PUBG_SEGMENTS_ROOT", str(root / "segments"))
    monkeypatch.setenv("PUBG_OWNER_LABELS_PATH", str(owner))
    return root


def test_pubg_keyboard_uses_game_prefix(pubg_data: Path) -> None:
    markup = inline_keyboard_markup("pubg", "abc123_42")
    row = markup["inline_keyboard"][0]
    assert row[0]["callback_data"] == "pubg_vseg_yes:abc123_42"
    assert row[1]["callback_data"] == "pubg_vseg_no:abc123_42"


def test_pubg_dislike_picker_not_metro_first(pubg_data: Path) -> None:
    markup = dislike_picker_markup("pubg", "abc123_42", callback_prefix="pubg_vseg_bad")
    assert markup["inline_keyboard"][0][0]["text"] == "🚇 Не Metro"
    assert markup["inline_keyboard"][0][0]["callback_data"] == "pubg_vseg_bad:abc123_42:not_metro"


def test_apply_owner_label_records_feedback(pubg_data: Path) -> None:
    upsert_segment(
        "pubg",
        {
            "segment_id": "abc123_42",
            "path": str(pubg_data / "segments" / "seg_abc123_42.mp4"),
            "vod": "/tmp/vod.mp4",
            "start": 42,
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
