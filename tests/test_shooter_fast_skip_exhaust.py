"""Regression: fast-skip must persist exhausted into registry."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from shooter_vod_segment_feed import _mark_fast_skip_exhausted  # noqa: E402


def test_fast_skip_appends_registry(tmp_path: Path) -> None:
    vod = tmp_path / "yt_eI-XQt5j_mk.mp4"
    vod.write_bytes(b"x")
    state: dict = {"vods": []}
    row = _mark_fast_skip_exhausted(
        state,
        vod,
        vid="eI-XQt5j_mk",
        title="test",
        fast_reason="fast_panns_0/2",
        entry=None,
    )
    assert row["exhausted"] is True
    assert state["vods"][0]["id"] == "eI-XQt5j_mk"
    assert state["vods"][0]["exhausted"] is True
