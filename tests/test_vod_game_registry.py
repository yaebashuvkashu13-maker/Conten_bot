"""Tests for vod_game_registry."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

from vod_game_registry import exhausted_summary, inbox_video_ids, spec  # noqa: E402


def test_specs_cover_daily_games() -> None:
    from vod_game_registry import VOD_GAMES, spec

    for g in VOD_GAMES:
        assert spec(g).id == g
    assert spec("mlbb").profile == "mobile_legends"
    assert spec("pubg").feed_kind == "shooter"
    assert spec("genshin").feed_kind == "extended"
    assert spec("wot").default_data_root.endswith("/wot")


def test_exhausted_summary_empty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "pubg"
    inbox = root / "youtube_nightly" / "inbox"
    inbox.mkdir(parents=True)
    (inbox / "yt_abc12345678.mp4").write_bytes(b"x")
    state_path = root / "vod_segment_state.json"
    state_path.write_text(
        json.dumps(
            {
                "vods": [
                    {"id": "abc12345678", "exhausted": True, "reject_reason": "metro_vod_reject"},
                    {"id": "other", "exhausted": True},
                ],
                "vod_outcomes": [{"id": "abc12345678", "sent": 0}],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("SHOOTER_PUBG_DATA_ROOT", str(root))
    row = exhausted_summary("pubg")
    assert row["inbox"] == 1
    assert row["exhausted_inbox"] == 1
