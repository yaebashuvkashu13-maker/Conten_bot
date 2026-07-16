"""Tests for structured long-scan heartbeat."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import vod_pipeline_heartbeat as hb  # noqa: E402


def test_heartbeat_records_stage_and_progress(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "heartbeat.json"
    monkeypatch.setenv("VOD_HEARTBEAT_PATH", str(path))
    hb._LAST_WRITE = 0
    hb.heartbeat(
        "banner_dense_scan",
        vod_id="abcdefghijk",
        progress=0.5,
        candidates_in=8,
        candidates_out=1,
        force=True,
    )
    data = json.loads(path.read_text())
    assert data["stage"] == "banner_dense_scan"
    assert data["vod_id"] == "abcdefghijk"
    assert data["progress"] == 0.5
    assert data["candidates_out"] == 1
