"""MLBB VOD feed state roundtrip via vod_state_io."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))


@pytest.fixture()
def feed_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    state_path = tmp_path / "vod_segment_state.json"
    monkeypatch.setattr("mlbb_vod_segment_feed.STATE_PATH", state_path)
    return state_path


def test_mlbb_vod_save_load_roundtrip(feed_state: Path) -> None:
    pytest.importorskip("cv2")
    from mlbb_vod_segment_feed import _load_state, _save_state

    _save_state({"vods": [{"id": "x"}], "used_youtube_ids": ["x"], "scanned_vods": []})
    data = _load_state()
    assert data["vods"][0]["id"] == "x"


def test_mlbb_vod_bak_recovery(feed_state: Path) -> None:
    pytest.importorskip("cv2")
    from mlbb_vod_segment_feed import _load_state, _save_state

    _save_state({"vods": [], "used_youtube_ids": [], "scanned_vods": [], "marker": 1})
    feed_state.write_text("CORRUPT", encoding="utf-8")
    data = _load_state()
    assert data.get("marker") == 1
