"""Tests for daily_game_cycle module."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import daily_game_cycle as cycle  # noqa: E402


@pytest.fixture
def isolated_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    state = tmp_path / "daily_game_cycle.json"
    monkeypatch.setenv("DAILY_GAME_CYCLE_STATE", str(state))
    monkeypatch.setenv("DAILY_GAME_CYCLE_ENABLED", "1")
    monkeypatch.setenv("DAILY_GAME_MLBB_QUOTA", "5")
    monkeypatch.setenv("DAILY_GAME_PUBG_QUOTA", "3")
    monkeypatch.setenv("DAILY_GAME_STANDOFF_QUOTA", "3")
    monkeypatch.setenv("DAILY_GAME_GENSHIN_QUOTA", "5")
    monkeypatch.setenv("DAILY_GAME_WOT_QUOTA", "3")
    return state


def test_active_game_starts_mlbb(isolated_state: Path) -> None:
    assert cycle.active_game() == "mlbb"
    assert cycle.quota_remaining("mlbb") == 5


def test_daily_game_quota_preferred_over_legacy(isolated_state: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DAILY_MLBB_QUOTA", "10")
    assert cycle.quota_for("mlbb") == 5


def test_mlbb_quota_advances_to_pubg(isolated_state: Path) -> None:
    for _ in range(5):
        cycle.record_send("mlbb", 1)
    assert cycle.quota_remaining("mlbb") == 0
    assert cycle.active_game() == "pubg"


def test_full_rotation(isolated_state: Path) -> None:
    for _ in range(5):
        cycle.record_send("mlbb", 1)
    for _ in range(3):
        cycle.record_send("pubg", 1)
    assert cycle.active_game() == "standoff"
    for _ in range(3):
        cycle.record_send("standoff", 1)
    assert cycle.active_game() == "genshin"
    for _ in range(5):
        cycle.record_send("genshin", 1)
    assert cycle.active_game() == "wot"
    for _ in range(3):
        cycle.record_send("wot", 1)
    assert cycle.active_game() is None


def test_can_send_blocks_wrong_game(isolated_state: Path) -> None:
    cycle.record_send("mlbb", 3)
    ok, reason = cycle.can_send_for_game("pubg", 1)
    assert ok is False
    assert "wait_for_mlbb" in reason


def test_reset_new_day(isolated_state: Path) -> None:
    cycle.record_send("mlbb", 5)
    assert cycle.active_game() == "pubg"
    with patch.object(cycle, "_today_key", return_value="2099-01-02"):
        assert cycle.reset_if_new_day() is True
        assert cycle.active_game() == "mlbb"
        assert cycle.send_count("mlbb") == 0


def test_stall_skip_advances_to_next_game(isolated_state: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DAILY_GAME_STALL_ZERO_RUNS", "3")
    monkeypatch.setenv("DAILY_GAME_STALL_MAX_SEC", "600")
    for _ in range(5):
        cycle.record_send("mlbb", 1)
    assert cycle.active_game() == "pubg"
    # Normal one-VOD rejects must NOT stall immediately.
    for _ in range(5):
        cycle.note_feed_iteration("pubg", 0, thrash=False)
    assert cycle.is_game_stalled("pubg") is False
    # Thrash + age over limit → stall.
    import time

    state = cycle.load_state()
    state["stall"]["pubg"]["since"] = time.time() - 700
    state["stall"]["pubg"]["thrash_runs"] = 3
    cycle.save_state(state)
    assert cycle.is_game_stalled("pubg") is True
    cycle.force_skip_game("pubg", reason="test")
    assert cycle.active_game() == "standoff"


def test_send_clears_stall(isolated_state: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DAILY_GAME_STALL_ZERO_RUNS", "3")
    for _ in range(5):
        cycle.record_send("mlbb", 1)
    cycle.note_feed_iteration("pubg", 0, thrash=True)
    cycle.note_feed_iteration("pubg", 0, thrash=True)
    cycle.note_feed_iteration("pubg", 1)
    assert cycle.is_game_stalled("pubg") is False
    assert cycle.active_game() == "pubg"


def test_today_key_uses_moscow(monkeypatch: pytest.MonkeyPatch) -> None:
    from datetime import datetime

    class FakeDatetime:
        @staticmethod
        def now(tz=None):
            return datetime(2026, 6, 26, 23, 30, tzinfo=tz)

    monkeypatch.setattr(cycle, "datetime", FakeDatetime)
    assert cycle._today_key() == "2026-06-26"


def test_disabled_cycle_allows_all(isolated_state: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DAILY_GAME_CYCLE_ENABLED", "0")
    ok, reason = cycle.can_send_for_game("standoff", 1)
    assert ok is True
    assert reason == "cycle_disabled"


def test_clear_stall_resumes_force_skipped_game(isolated_state: Path) -> None:
    for _ in range(5):
        cycle.record_send("mlbb", 1)
    cycle.force_skip_game("pubg", reason="test")
    assert cycle.is_game_stalled("pubg") is True
    assert cycle.active_game() == "standoff"
    cleared = cycle.clear_stall("pubg", reason="unit")
    assert cleared == ["pubg"]
    assert cycle.is_game_stalled("pubg") is False
    assert cycle.active_game() == "pubg"


def test_clear_stall_preserves_timeout_streak_on_auto_unstall(
    isolated_state: Path,
) -> None:
    """SLA/self-heal must not reset timeout_runs — else skip-after never trips."""
    for _ in range(5):
        cycle.record_send("mlbb", 1)
    for _ in range(3):
        cycle.record_send("pubg", 1)
    for _ in range(3):
        cycle.record_send("standoff", 1)
    cycle.note_feed_iteration("genshin", 0, timed_out=True)
    entry = (cycle.load_state().get("stall") or {}).get("genshin") or {}
    assert int(entry.get("timeout_runs") or 0) == 1

    assert cycle.clear_stall("genshin", reason="hourly_sla_local_media") == []
    assert cycle.clear_stall("genshin", reason="self_heal_local_media") == []
    assert cycle.clear_stall("genshin", reason="inbox_ready_unstall") == []
    entry = (cycle.load_state().get("stall") or {}).get("genshin") or {}
    assert int(entry.get("timeout_runs") or 0) == 1

    cleared = cycle.clear_stall("genshin", reason="manual_clear")
    assert cleared == ["genshin"]
    entry = (cycle.load_state().get("stall") or {}).get("genshin") or {}
    assert int(entry.get("timeout_runs") or 0) == 0


def test_clear_stall_noop_keeps_all_stalled_notify_key(isolated_state: Path) -> None:
    """No-op auto-unstall must not wipe anti-spam keys (TG spam every ~45s)."""
    for _ in range(5):
        cycle.record_send("mlbb", 1)
    for _ in range(3):
        cycle.record_send("pubg", 1)
    for _ in range(3):
        cycle.record_send("standoff", 1)
    cycle.note_feed_iteration("genshin", 0, timed_out=True)
    state = cycle.load_state()
    state.setdefault("notified", {})["all_stalled:2099-01-01"] = "2026-08-10 07:00:00"
    state["notified"]["stall_skip:genshin"] = "2026-08-10 07:00:00"
    cycle.save_state(state)

    assert cycle.clear_stall("genshin", reason="inbox_ready_unstall") == []
    notified = cycle.load_state().get("notified") or {}
    assert notified.get("all_stalled:2099-01-01") == "2026-08-10 07:00:00"
    assert notified.get("stall_skip:genshin") == "2026-08-10 07:00:00"

    assert cycle.clear_stall("genshin", reason="manual_clear") == ["genshin"]
    notified = cycle.load_state().get("notified") or {}
    assert "all_stalled:2099-01-01" not in notified
    assert "stall_skip:genshin" not in notified


def test_unstall_on_inbox_ready(isolated_state: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for _ in range(5):
        cycle.record_send("mlbb", 1)
    cycle.force_skip_game("pubg", reason="test")
    inbox = tmp_path / "pubg_inbox"
    inbox.mkdir()
    (inbox / "yt_abc.mp4").write_bytes(b"x")
    monkeypatch.setenv("DAILY_GAME_UNSTALL_ON_INBOX", "1")
    with patch.object(cycle, "_game_inbox_ready", side_effect=lambda g: g == "pubg"):
        assert cycle.active_game() == "pubg"
    assert cycle.is_game_stalled("pubg") is False


def test_montage_soft_min_allows_partial(monkeypatch: pytest.MonkeyPatch) -> None:
    import shooter_vod_segment_feed as feed

    monkeypatch.setenv("SHOOTER_VOD_MONTAGE_MIN_CLIPS", "3")
    monkeypatch.setenv("SHOOTER_VOD_MONTAGE_SHIP_PARTIAL", "1")
    monkeypatch.setenv("SHOOTER_VOD_MONTAGE_SOFT_MIN_CLIPS", "1")
    monkeypatch.delenv("PUBG_VOD_MONTAGE_SOFT_MIN_CLIPS", raising=False)
    monkeypatch.delenv("WOT_VOD_MONTAGE_SOFT_MIN_CLIPS", raising=False)
    monkeypatch.delenv("STANDOFF_VOD_MONTAGE_SOFT_MIN_CLIPS", raising=False)
    assert feed._montage_limits()[0] == 3
    # OWNER CONTRACT: all combat shooters full ×3 (quota lowered for склейки).
    assert feed._montage_soft_min_clips("pubg") == 3
    assert feed._montage_soft_min_clips("wot") == 3
    assert feed._montage_soft_min_clips("standoff") == 3
    monkeypatch.setenv("PUBG_VOD_MONTAGE_SOFT_MIN_CLIPS", "4")
    assert feed._montage_soft_min_clips("pubg") == 4
    monkeypatch.delenv("PUBG_VOD_MONTAGE_SOFT_MIN_CLIPS", raising=False)
    monkeypatch.setenv("SHOOTER_VOD_MONTAGE_SHIP_PARTIAL", "0")
    assert feed._montage_soft_min_clips("pubg") == 3
    assert feed._montage_soft_min_clips("wot") == 3
    assert feed._montage_soft_min_clips("standoff") == 3
