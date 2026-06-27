"""PUBG owner learning — VOD segment time anchors."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from pubg_owner_learning import (  # noqa: E402
    OWNER_PATH,
    sync_vod_segment_to_owner_json,
)


@pytest.fixture
def owner_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    path = tmp_path / "pubg_owner_labels.json"
    monkeypatch.setenv("PUBG_OWNER_LABELS_PATH", str(path))
    return path


def test_vod_segment_good_writes_time_anchor(owner_store: Path) -> None:
    ok = sync_vod_segment_to_owner_json("abc123", 507.0, is_good=True, segment_id="abc123_507")
    assert ok is True
    data = json.loads(owner_store.read_text(encoding="utf-8"))
    rows = data["videos"]["abc123"]
    assert rows[0]["time_sec"] == 507.0
    assert rows[0]["label"] == "good"
    assert rows[0]["source"] == "vod_segment"
