"""Tests for used_youtube_ids trim — unblock YouTube discovery."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

from vod_feed_recover import auto_heal_stalled_feed, should_auto_heal  # noqa: E402
from vod_game_registry import load_state, save_state, trim_used_youtube_ids  # noqa: E402


def test_trim_aggressive_clears_stale_ids(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "pubg"
    inbox = root / "youtube_nightly" / "inbox"
    inbox.mkdir(parents=True)
    (inbox / "yt_keep1111111.mp4").write_bytes(b"x")
    state_path = root / "vod_segment_state.json"
    state_path.write_text(
        json.dumps(
            {
                "used_youtube_ids": ["keep1111111", "stale2222222", "stale3333333"],
                "vods": [{"id": "keep1111111", "exhausted": False}],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("SHOOTER_PUBG_DATA_ROOT", str(root))
    state = load_state("pubg")
    removed = trim_used_youtube_ids(state, "pubg", aggressive=True)
    assert removed == 2
    assert state["used_youtube_ids"] == ["keep1111111"]


def test_trim_auto_when_over_max(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "pubg"
    inbox = root / "youtube_nightly" / "inbox"
    inbox.mkdir(parents=True)
    ids = [f"id{i:07d}aa"[:11] for i in range(250)]
    state_path = root / "vod_segment_state.json"
    state_path.write_text(json.dumps({"used_youtube_ids": ids, "vods": []}), encoding="utf-8")
    monkeypatch.setenv("SHOOTER_PUBG_DATA_ROOT", str(root))
    monkeypatch.setenv("SHOOTER_VOD_USED_IDS_MAX", "100")
    state = load_state("pubg")
    removed = trim_used_youtube_ids(state, "pubg")
    assert removed == 250
    assert state["used_youtube_ids"] == []


def test_montage_soft_min_partial_ship(monkeypatch: pytest.MonkeyPatch) -> None:
    from shooter_vod_segment_feed import _montage_soft_min_clips  # noqa: E402

    monkeypatch.setenv("SHOOTER_VOD_MONTAGE_MIN_CLIPS", "3")
    monkeypatch.setenv("PUBG_VOD_MONTAGE_SOFT_MIN_CLIPS", "1")
    monkeypatch.setenv("VOD_PUBG_QUALITY_STRICT", "0")
    monkeypatch.setenv("SHOOTER_VOD_MONTAGE_SHIP_PARTIAL", "1")
    assert _montage_soft_min_clips("pubg") == 2

    monkeypatch.setenv("VOD_PUBG_QUALITY_STRICT", "1")
    assert _montage_soft_min_clips("pubg") == 3


def test_should_auto_heal_on_bloated_used_ids(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "pubg"
    root.mkdir()
    state_path = root / "vod_segment_state.json"
    state_path.write_text(
        json.dumps({"used_youtube_ids": [f"id{i:07d}aa"[:11] for i in range(250)], "vods": []}),
        encoding="utf-8",
    )
    monkeypatch.setenv("SHOOTER_PUBG_DATA_ROOT", str(root))
    monkeypatch.setenv("SHOOTER_VOD_USED_IDS_MAX", "100")
    state = load_state("pubg")
    ok, reason = should_auto_heal("pubg", state)
    assert ok is True
    assert "used_ids" in reason


def test_auto_heal_trims_used_ids(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "pubg"
    root.mkdir()
    state_path = root / "vod_segment_state.json"
    state_path.write_text(
        json.dumps({"used_youtube_ids": [f"id{i:07d}aa"[:11] for i in range(250)], "vods": []}),
        encoding="utf-8",
    )
    monkeypatch.setenv("SHOOTER_PUBG_DATA_ROOT", str(root))
    monkeypatch.setenv("SHOOTER_VOD_USED_IDS_MAX", "100")
    stats = auto_heal_stalled_feed("pubg")
    assert stats.get("healed") == 1
    state = load_state("pubg")
    assert state.get("used_youtube_ids") == []
