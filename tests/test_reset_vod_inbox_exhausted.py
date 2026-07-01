"""Tests for reset_vod_inbox_exhausted."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

from reset_vod_inbox_exhausted import reset_game  # noqa: E402


def test_reset_clears_exhausted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "standoff"
    inbox = root / "youtube_nightly" / "inbox"
    inbox.mkdir(parents=True)
    (inbox / "yt_standoff1234.mp4").write_bytes(b"x")
    state_path = root / "vod_segment_state.json"
    state_path.write_text(
        json.dumps({"vods": [{"id": "standoff1234", "exhausted": True, "reject_reason": "none"}]}),
        encoding="utf-8",
    )
    monkeypatch.setenv("SHOOTER_STANDOFF_DATA_ROOT", str(root))
    n = reset_game("standoff", dry_run=False)
    assert n == 1
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["vods"][0]["exhausted"] is False
    assert "reject_reason" not in state["vods"][0]
