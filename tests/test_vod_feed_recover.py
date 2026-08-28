"""VOD feed recover — locks, pauses, cooldowns, supervisor restart."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

from vod_feed_recover import (  # noqa: E402
    bump_scan_cooldowns,
    clear_discovery_pauses,
    clear_feed_locks,
    run_recover,
)


def test_clear_feed_locks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    lock = tmp_path / "pubg_vod_segment_feed.lock"
    lock.write_text("1", encoding="utf-8")
    monkeypatch.setattr(
        "vod_feed_recover.feed_lock_paths",
        lambda: [lock],
    )
    removed = clear_feed_locks()
    assert removed == ["pubg_vod_segment_feed.lock"]
    assert not lock.exists()


def test_clear_discovery_pauses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "pubg"
    root.mkdir()
    state_path = root / "vod_segment_state.json"
    state_path.write_text(
        json.dumps({"discovery_pause_until": 9999999999.0, "vods": []}),
        encoding="utf-8",
    )
    monkeypatch.setenv("SHOOTER_PUBG_DATA_ROOT", str(root))
    assert clear_discovery_pauses("pubg") is True
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert "discovery_pause_until" not in state


def test_bump_scan_cooldowns(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "pubg"
    inbox = root / "youtube_nightly" / "inbox"
    inbox.mkdir(parents=True)
    (inbox / "yt_abc123xyz00.mp4").write_bytes(b"x")
    state_path = root / "vod_segment_state.json"
    state_path.write_text(
        json.dumps(
            {
                "vods": [
                    {
                        "id": "abc123xyz00",
                        "last_scan_at": 1.0,
                        "last_scan_blocked": True,
                        "reject_reason": "not_metro",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("SHOOTER_PUBG_DATA_ROOT", str(root))
    n = bump_scan_cooldowns("pubg")
    assert n == 1
    row = json.loads(state_path.read_text(encoding="utf-8"))["vods"][0]
    assert "last_scan_at" not in row
    assert "reject_reason" not in row


def test_run_recover_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "pubg"
    inbox = root / "youtube_nightly" / "inbox"
    inbox.mkdir(parents=True)
    (inbox / "yt_abc123xyz00.mp4").write_bytes(b"x")
    state_path = root / "vod_segment_state.json"
    state_path.write_text(
        json.dumps(
            {
                "vods": [{"id": "abc123xyz00", "exhausted": True, "reject_reason": "not_metro"}],
                "discovery_pause_until": 9999999999.0,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("SHOOTER_PUBG_DATA_ROOT", str(root))
    msg = run_recover(
        "pubg",
        restart=lambda **_: (True, "test restart"),
        probe=lambda: {"vod_supervisor": True, "daily_cycle": True, "shooter_feed": True, "telegram_bot": True},
    )
    assert "🔧 Восстановление" in msg
    assert "test restart" in msg
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["vods"][0]["exhausted"] is False
    assert "discovery_pause_until" not in state
